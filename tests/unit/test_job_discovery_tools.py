# -*- coding: utf-8 -*-
"""Tests for the deterministic job discovery tools (Phase 4).

Every test verifies that the tool is:
1. Deterministic — same input -> same output
2. Pure — no side effects
3. Graceful — empty/missing/bad input produces structured defaults
"""

import hashlib
import struct
import zlib

from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    DiscoveryRunResult,
    NormalizedJobCandidate,
    OcrResult,
    PageEvidence,
    TriageResult,
    WechatArticleResult,
)
from backend.app.services.job_discovery.tools import (
    build_candidate_idempotency_key,
    build_similarity_group_key,
    extract_jd_candidates,
    ocr_image,
    parse_wechat_article,
    triage_link,
    verify_evidence,
)


# ============================================================================
#  Link Triage Tests
# ============================================================================


class TestLinkTriage:
    def test_empty_url(self):
        result = triage_link("")
        assert result.site_type == "invalid"
        assert result.recommended_action == "skip"
        assert result.confidence == 1.0

    def test_whitespace_url(self):
        result = triage_link("  ")
        assert result.site_type == "invalid"

    def test_mailto_url(self):
        result = triage_link("mailto:hr@example.com")
        assert result.site_type == "email_only"
        assert result.recommended_action == "finish_manual_review"
        assert result.confidence == 1.0

    def test_non_http_url(self):
        result = triage_link("ftp://files.example.com/job.pdf")
        assert result.site_type == "invalid"
        assert result.recommended_action == "skip"

    def test_wechat_article(self):
        result = triage_link("https://mp.weixin.qq.com/s/abc123def456")
        assert result.site_type == "wechat_article"
        assert result.recommended_action == "parse_wechat_article"
        assert result.confidence == 1.0

    def test_blocked_domain(self):
        result = triage_link("https://www.linkedin.com/jobs/view/123")
        assert result.site_type == "blocked"
        assert result.recommended_action == "finish_manual_review"
        assert result.confidence == 0.95

    def test_blocked_zhaopin(self):
        result = triage_link("https://www.zhaopin.com/position/123")
        assert result.site_type == "blocked"

    def test_blocked_liepin(self):
        result = triage_link("https://www.liepin.com/job/123.shtml")
        assert result.site_type == "blocked"

    def test_blocked_51job(self):
        result = triage_link("https://www.51job.com/job/123.html")
        assert result.site_type == "blocked"

    def test_job_detail_with_id(self):
        result = triage_link("https://careers.example.com/job/12345")
        assert result.site_type == "job_detail"
        assert result.recommended_action == "run_web_navigation"

    def test_job_detail_position(self):
        result = triage_link("https://example.com/position/9876")
        assert result.site_type == "job_detail"

    def test_job_detail_requisition(self):
        result = triage_link("https://example.com/req/R12345")
        assert result.site_type == "job_detail"

    def test_job_detail_jd(self):
        result = triage_link("https://example.com/jd/888")
        assert result.site_type == "job_detail"

    def test_career_listing_jobs(self):
        result = triage_link("https://example.com/jobs")
        assert result.site_type == "career_site"
        assert result.recommended_action == "run_web_navigation"

    def test_career_listing_careers(self):
        result = triage_link("https://example.com/careers")
        assert result.site_type == "career_site"

    def test_career_listing_campus(self):
        result = triage_link("https://example.com/campus")
        assert result.site_type == "career_site"

    def test_career_listing_recruit(self):
        result = triage_link("https://example.com/recruit")
        assert result.site_type == "career_site"

    def test_career_listing_chinese(self):
        result = triage_link("https://example.com/社会招聘")
        assert result.site_type == "career_site"

    def test_official_site_homepage(self):
        result = triage_link("https://www.example.com")
        assert result.site_type == "official_site"
        assert result.recommended_action == "run_web_navigation"
        assert result.confidence == 0.60

    def test_official_site_about(self):
        result = triage_link("https://www.example.com/about")
        assert result.site_type == "official_site"

    def test_deterministic(self):
        url = "https://careers.tencent.com/job/12345"
        r1 = triage_link(url)
        r2 = triage_link(url)
        assert r1.site_type == r2.site_type
        assert r1.confidence == r2.confidence
        assert r1.recommended_action == r2.recommended_action

    def test_url_parse_error_handling(self):
        """URLs with exotic characters should not crash."""
        result = triage_link("https://example.com/\x00bad")
        assert isinstance(result, TriageResult)


# ============================================================================
#  WeChat Article Parser Tests
# ============================================================================


class TestWechatArticleParser:
    def test_empty_html(self):
        result = parse_wechat_article("", "https://mp.weixin.qq.com/s/test")
        assert isinstance(result, WechatArticleResult)
        assert result.title is None
        assert result.text_content == ""
        assert result.needs_manual_review is False

    def test_empty_url(self):
        result = parse_wechat_article("<html></html>", "")
        assert isinstance(result, WechatArticleResult)

    def test_title_from_og(self):
        html = """<html><head>
            <meta property="og:title" content="2026 Internship Program" />
            <title>Fallback Title</title>
        </head><body></body></html>"""
        result = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert result.title == "2026 Internship Program"

    def test_title_from_html_title(self):
        html = """<html><head>
            <title>Career Opportunities 2026</title>
        </head><body></body></html>"""
        result = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert result.title == "Career Opportunities 2026"

    def test_content_extraction(self):
        html = """<html><body>
            <div id="js_content">
                <p>This is the article content.</p>
                <p>Second paragraph with job description.</p>
            </div>
        </body></html>"""
        result = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert "article content" in result.text_content
        assert "job description" in result.text_content

    def test_image_extraction(self):
        html = """<html><body>
            <div id="js_content">
                <img data-src="https://mmbiz.qpic.cn/img1" />
                <img src="https://mmbiz.qpic.cn/img2" />
            </div>
        </body></html>"""
        result = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert len(result.image_urls) >= 2

    def test_email_delivery_instructions(self):
        html = """<html><body>
            <div id="js_content">
                <p>请将简历发送至 hr@tencent.com 参加本次招聘。</p>
            </div>
        </body></html>"""
        result = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert result.email_delivery_instructions is not None
        assert "hr@tencent.com" in result.email_delivery_instructions

    def test_email_delivery_alternative_keyword(self):
        html = """<html><body>
            <div id="js_content">
                <p>投递邮箱：career@example.com，邮件主题请注明岗位。</p>
            </div>
        </body></html>"""
        result = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert result.email_delivery_instructions is not None
        assert "career@example.com" in result.email_delivery_instructions

    def test_inaccessible_marker_detection(self):
        html = """<html><body>
            <div id="js_content">
                <p>请在微信客户端打开查看完整内容。</p>
            </div>
        </body></html>"""
        result = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert result.needs_manual_review is True
        assert "请在微信客户端打开" in result.manual_review_reason

    def test_no_email_no_content(self):
        html = """<html><body>
            <div id="js_content">
                <p>欢迎关注我们的公众号。</p>
            </div>
        </body></html>"""
        result = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert result.email_delivery_instructions is None
        assert result.needs_manual_review is False

    def test_deterministic(self):
        html = """<html><head>
            <meta property="og:title" content="Test Title" />
        </head><body>
            <div id="js_content"><p>Content here.</p></div>
        </body></html>"""
        r1 = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        r2 = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert r1.title == r2.title
        assert r1.text_content == r2.text_content


# ============================================================================
#  OCR Pipeline Tests
# ============================================================================


def _make_minimal_png(width: int, height: int) -> bytes:
    """Create a minimal valid PNG image with given dimensions."""
    signature = b"\x89PNG\r\n\x1a\n"

    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_len = struct.pack(">I", 13)
    ihdr_type = b"IHDR"
    ihdr_crc = struct.pack(">I", zlib.crc32(ihdr_type + ihdr_data) & 0xFFFFFFFF)

    raw_data = b""
    for _ in range(height):
        raw_data += b"\x00"
        raw_data += b"\x00\x00\x00" * width

    compressed = zlib.compress(raw_data)
    idat_len = struct.pack(">I", len(compressed))
    idat_type = b"IDAT"
    idat_crc = struct.pack(">I", zlib.crc32(idat_type + compressed) & 0xFFFFFFFF)

    iend_type = b"IEND"
    iend_crc = struct.pack(">I", zlib.crc32(iend_type) & 0xFFFFFFFF)

    return (
        signature
        + ihdr_len + ihdr_type + ihdr_data + ihdr_crc
        + idat_len + idat_type + compressed + idat_crc
        + struct.pack(">I", 0) + iend_type + iend_crc
    )


def _make_minimal_jpeg(width: int, height: int) -> bytes:
    """Create a minimal valid JPEG image with given dimensions."""
    buf = bytearray()
    buf.extend(b"\xff\xd8")
    app0_data = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    buf.extend(b"\xff\xe0")
    buf.extend(struct.pack(">H", len(app0_data) + 2))
    buf.extend(app0_data)
    sof_data = struct.pack(">BHHBB", 8, height, width, 3, 1)
    buf.extend(b"\xff\xc0")
    buf.extend(struct.pack(">H", len(sof_data) + 2))
    buf.extend(sof_data)
    buf.extend(b"\xff\xda\x00\x08\x01\x03\x00\x00\x3f\x00")
    buf.extend(b"\xff\xd9")
    return bytes(buf)


class TestOcrPipeline:
    def test_ocr_disabled(self):
        result = ocr_image(b"some bytes", ocr_enabled=False)
        assert result.needs_manual_review is True
        assert "OCR disabled" in " ".join(result.warnings)
        assert result.full_text == ""

    def test_empty_bytes(self):
        result = ocr_image(b"", ocr_enabled=True)
        assert result.needs_manual_review is True
        assert result.full_text == ""

    def test_png_dimension_parsing(self):
        png_bytes = _make_minimal_png(100, 200)
        result = ocr_image(png_bytes, ocr_enabled=True)
        assert isinstance(result, OcrResult)
        assert "No OCR engine available" in " ".join(result.warnings)

    def test_tall_image_slicing(self):
        png_bytes = _make_minimal_png(800, 3000)
        result = ocr_image(png_bytes, ocr_enabled=True)
        warnings_text = " ".join(result.warnings)
        assert "overlapping slices" in warnings_text or "3000px" in warnings_text

    def test_jpeg_dimension_parsing(self):
        jpeg_bytes = _make_minimal_jpeg(640, 480)
        result = ocr_image(jpeg_bytes, ocr_enabled=True)
        assert isinstance(result, OcrResult)

    def test_unsupported_format(self):
        result = ocr_image(b"\x00\x01\x02\x03not an image", ocr_enabled=True)
        warnings_text = " ".join(result.warnings)
        assert "Could not parse image dimensions" in warnings_text

    def test_deterministic(self):
        png_bytes = _make_minimal_png(10, 10)
        r1 = ocr_image(png_bytes, ocr_enabled=True)
        r2 = ocr_image(png_bytes, ocr_enabled=True)
        assert r1.needs_manual_review == r2.needs_manual_review
        assert r1.warnings == r2.warnings


# ============================================================================
#  JD Extraction Tests
# ============================================================================


class TestJDExtraction:
    def test_empty_text(self):
        result = extract_jd_candidates("", "https://example.com/job/1")
        assert result == []

    def test_whitespace_text(self):
        result = extract_jd_candidates("   \n  \t  ", "https://example.com/job/1")
        assert result == []

    def test_chinese_jd_with_all_fields(self):
        jd_text = """
        岗位名称：高级后端开发工程师
        公司名称：腾讯科技
        所属部门：微信事业群

        岗位职责：
        负责微信后端服务的设计、开发和维护。
        参与系统架构优化，提升系统性能和稳定性。

        任职要求：
        计算机相关专业本科及以上学历。
        5年以上后端开发经验，精通Python/Go。

        工作地点：深圳
        截止日期：2026年12月31日
        内推码：NTAD123
        """
        result = extract_jd_candidates(jd_text, "https://careers.tencent.com/job/123")
        assert len(result) == 1
        candidate = result[0]
        assert candidate.title == "高级后端开发工程师"
        assert candidate.company_name == "腾讯科技"
        assert candidate.department == "微信事业群"
        assert "微信后端服务" in candidate.responsibilities
        assert "计算机相关专业" in candidate.requirements
        assert "深圳" in candidate.locations
        assert candidate.deadline_text is not None
        assert "2026" in candidate.deadline_text
        assert candidate.referral_code == "NTAD123"
        assert candidate.confidence > 0.5

    def test_english_jd(self):
        jd_text = """
        Job Title: Software Engineer Intern

        Responsibilities:
        Build and maintain cloud infrastructure.
        Collaborate with cross-functional teams.
        Write unit tests and integration tests.

        Requirements:
        Currently pursuing BS/MS in CS or related field.
        Proficiency in Python or Java.
        Strong problem-solving skills.

        Location: Shanghai
        """
        result = extract_jd_candidates(jd_text, "https://example.com/job/2")
        assert len(result) == 1
        candidate = result[0]
        assert candidate.title is not None
        assert any(x in (candidate.title or "").lower() for x in ["software engineer", "intern", "software"])
        assert "Shanghai" in candidate.locations
        assert candidate.confidence > 0.5

    def test_jd_with_email_apply(self):
        jd_text = """
        职位名称：产品经理
        公司名称：字节跳动

        岗位职责：
        负责产品的需求分析和功能设计。

        任职要求：
        3年以上产品经理经验。

        投递方式：请将简历发送至 hiring@bytedance.com
        工作地点：北京
        """
        result = extract_jd_candidates(jd_text, "https://example.com/job/3")
        assert len(result) == 1
        candidate = result[0]
        assert candidate.application_channel_json is not None
        assert candidate.application_channel_json.get("method") == "email"
        assert candidate.application_channel_json.get("gui_eligible") is False

    def test_jd_with_recruitment_types(self):
        jd_text = """
        岗位名称：前端开发实习生
        公司名称：阿里巴巴

        岗位职责：
        参与前端项目的开发和维护。

        任职要求：
        2027届毕业生，计算机相关专业。

        工作地点：杭州
        """
        result = extract_jd_candidates(jd_text, "https://example.com/job/4")
        assert len(result) == 1
        candidate = result[0]
        assert "internship" in candidate.recruitment_types

    def test_minimal_jd(self):
        """Very short text should still produce a candidate with low confidence."""
        result = extract_jd_candidates(
            "招聘岗位：Python工程师",
            "https://example.com/job/5",
        )
        assert len(result) == 1
        candidate = result[0]
        assert len(candidate.normalization_warnings) > 0

    def test_deterministic(self):
        jd_text = """
        岗位名称：测试工程师
        公司名称：测试公司
        岗位职责：负责测试工作。
        任职要求：有测试经验。
        """
        r1 = extract_jd_candidates(jd_text, "https://example.com/job/6")
        r2 = extract_jd_candidates(jd_text, "https://example.com/job/6")
        assert len(r1) == len(r2)
        assert r1[0].title == r2[0].title
        assert r1[0].confidence == r2[0].confidence


# ============================================================================
#  Evidence Verifier Tests
# ============================================================================


class TestEvidenceVerifier:
    def test_empty_lists(self):
        result = verify_evidence([], [])
        assert result == []

    def test_reject_no_title_no_company(self):
        candidates = [
            NormalizedJobCandidate(
                title=None,
                company_name=None,
                description_text="some text",
                evidence_refs=[{"type": "page_text", "url": "https://example.com"}],
            )
        ]
        result = verify_evidence(candidates, [])
        assert len(result) == 0

    def test_reject_no_evidence_refs(self):
        candidates = [
            NormalizedJobCandidate(
                title="Engineer",
                company_name="Company",
                description_text="some text",
                evidence_refs=[],
            )
        ]
        result = verify_evidence(candidates, [])
        assert len(result) == 0

    def test_accept_with_evidence_refs(self):
        candidates = [
            NormalizedJobCandidate(
                title="Engineer",
                company_name="Company",
                description_text="Full job description with duties and requirements.",
                evidence_refs=[{"type": "page_text", "url": "https://example.com"}],
            )
        ]
        result = verify_evidence(candidates, [])
        assert len(result) == 1

    def test_accept_with_evidence_list(self):
        """Candidate without refs but with evidence list should pass with a warning."""
        candidates = [
            NormalizedJobCandidate(
                title="Engineer",
                company_name="Company",
                description_text="Full job description with duties and requirements.",
                evidence_refs=[],
            )
        ]
        evidence = [
            PageEvidence(
                evidence_type="page_text",
                url="https://example.com",
                content_hash="abc123",
            )
        ]
        result = verify_evidence(candidates, evidence)
        assert len(result) == 1
        assert any("no evidence_refs" in w.lower() for w in result[0].normalization_warnings)

    def test_flag_vague_description(self):
        candidates = [
            NormalizedJobCandidate(
                title="Engineer",
                company_name="Company",
                description_text="Short",
                evidence_refs=[{"type": "page_text", "url": "https://example.com"}],
            )
        ]
        result = verify_evidence(candidates, [])
        assert len(result) == 1
        assert any("vague" in w.lower() for w in result[0].normalization_warnings)

    def test_flag_stale_content(self):
        candidates = [
            NormalizedJobCandidate(
                title="Engineer",
                company_name="Company",
                description_text="Join us in 2020 for an exciting career. " * 10,
                evidence_refs=[{"type": "page_text", "url": "https://example.com"}],
            )
        ]
        result = verify_evidence(candidates, [])
        assert len(result) == 1
        assert any("stale" in w.lower() for w in result[0].normalization_warnings)

    def test_keeps_valid_candidates(self):
        candidates = [
            NormalizedJobCandidate(
                title="Engineer",
                company_name="Company",
                description_text="A real job description with lots of useful content.",
                evidence_refs=[{"type": "page_text", "url": "https://example.com"}],
            ),
            NormalizedJobCandidate(
                title=None,
                company_name=None,
                description_text="bad",
                evidence_refs=[],
            ),
        ]
        result = verify_evidence(candidates, [])
        assert len(result) == 1
        assert result[0].title == "Engineer"

    def test_does_not_mutate_originals(self):
        candidate = NormalizedJobCandidate(
            title="Engineer",
            company_name="Company",
            description_text="A real job description with lots of useful content.",
            evidence_refs=[{"type": "page_text", "url": "https://example.com"}],
            normalization_warnings=["original warning"],
        )
        original_warnings = list(candidate.normalization_warnings)
        verify_evidence([candidate], [])
        assert candidate.normalization_warnings == original_warnings

    def test_deterministic(self):
        candidates = [
            NormalizedJobCandidate(
                title="Engineer",
                company_name="Company",
                description_text="A real job description with lots of useful content.",
                evidence_refs=[{"type": "page_text", "url": "https://example.com"}],
            )
        ]
        r1 = verify_evidence(candidates, [])
        r2 = verify_evidence(candidates, [])
        assert len(r1) == len(r2)
        assert r1[0].title == r2[0].title


# ============================================================================
#  Candidate Packager Tests
# ============================================================================


class TestCandidatePackager:
    def test_idempotency_key_deterministic(self):
        key1 = build_candidate_idempotency_key(
            "Tencent", "Engineer", "Shenzhen",
            "https://careers.tencent.com/job/1", "abc123",
        )
        key2 = build_candidate_idempotency_key(
            "Tencent", "Engineer", "Shenzhen",
            "https://careers.tencent.com/job/1", "abc123",
        )
        assert key1 == key2
        assert len(key1) == 64

    def test_idempotency_key_case_insensitive(self):
        key1 = build_candidate_idempotency_key(
            "TENCENT", "ENGINEER", "SHENZHEN",
            "HTTPS://CAREERS.TENCENT.COM/JOB/1", "ABC123",
        )
        key2 = build_candidate_idempotency_key(
            "tencent", "engineer", "shenzhen",
            "https://careers.tencent.com/job/1", "abc123",
        )
        assert key1 == key2

    def test_idempotency_key_different_inputs_different_keys(self):
        key1 = build_candidate_idempotency_key(
            "Tencent", "Engineer", "Shenzhen",
            "https://careers.tencent.com/job/1", "abc123",
        )
        key2 = build_candidate_idempotency_key(
            "Alibaba", "Engineer", "Beijing",
            "https://careers.alibaba.com/job/2", "def456",
        )
        assert key1 != key2

    def test_idempotency_key_empty_fields(self):
        key = build_candidate_idempotency_key(
            "", "", "", "", "",
        )
        assert len(key) == 64
        key2 = build_candidate_idempotency_key(
            "", "", "", "", "",
        )
        assert key == key2

    def test_similarity_group_key_deterministic(self):
        key1 = build_similarity_group_key(
            "Tencent", "Software Engineer", "internship", "web",
        )
        key2 = build_similarity_group_key(
            "Tencent", "Software Engineer", "internship", "web",
        )
        assert key1 == key2

    def test_similarity_group_key_prefix(self):
        key = build_similarity_group_key(
            "Tencent", "Software Engineer", "internship", "web",
        )
        assert key.startswith("ten::sof::")

    def test_similarity_group_key_case_insensitive(self):
        key1 = build_similarity_group_key(
            "TENCENT", "SOFTWARE ENGINEER", "INTERNSHIP", "WEB",
        )
        key2 = build_similarity_group_key(
            "tencent", "Software Engineer", "internship", "web",
        )
        assert key1 == key2

    def test_similarity_group_key_empty_prefixes(self):
        key = build_similarity_group_key("", "", "", "")
        assert key == "::::unknown::unknown"

    def test_similarity_group_key_similar_roles(self):
        """Roles with same prefix should get same group key."""
        key1 = build_similarity_group_key(
            "Tencent", "Software Engineer", "full_time", "web",
        )
        key2 = build_similarity_group_key(
            "Tencent", "Software Developer", "full_time", "web",
        )
        assert key1 == key2


# ============================================================================
#  Schema Tests (basic validation)
# ============================================================================


class TestSchemas:
    def test_discovery_task_input_defaults(self):
        dti = DiscoveryTaskInput(
            source_id="s1",
            raw_record_id="r1",
            external_record_id="e1",
            source_key="tencent-27-referrals",
            source_url="https://example.com",
            url_hash="abc",
            record_fields=[],
        )
        assert dti.source_id == "s1"
        assert dti.record_fields == []

    def test_triage_result_defaults(self):
        tr = TriageResult(site_type="invalid", confidence=0.0, recommended_action="skip")
        assert tr.notes == ""

    def test_page_evidence_defaults(self):
        pe = PageEvidence(evidence_type="page_text")
        assert pe.content_hash == ""
        assert pe.url is None

    def test_wechat_article_result_defaults(self):
        war = WechatArticleResult()
        assert war.text_content == ""
        assert war.image_urls == []
        assert war.needs_manual_review is False

    def test_ocr_result_defaults(self):
        ocr = OcrResult()
        assert ocr.full_text == ""
        assert ocr.warnings == []
        assert ocr.needs_manual_review is False

    def test_normalized_job_candidate_defaults(self):
        njc = NormalizedJobCandidate()
        assert njc.title is None
        assert njc.locations == []
        assert njc.evidence_refs == []
        assert njc.normalization_warnings == []

    def test_discovery_run_result_defaults(self):
        drr = DiscoveryRunResult(status="succeeded")
        assert drr.evidence == []
        assert drr.candidates == []
        assert drr.summary == ""


# ============================================================================
#  Integration-style: end-to-end deterministic pipeline
# ============================================================================


class TestEndToEndDeterministic:
    def test_full_pipeline_determinism(self):
        """Run the full chain with known inputs and verify determinism."""
        url = "https://careers.tencent.com/job/12345"

        triage = triage_link(url)
        assert triage.site_type == "job_detail"

        jd_text = """
        岗位名称：后端开发工程师
        公司名称：腾讯科技
        岗位职责：负责后端服务开发。
        任职要求：3年以上经验。
        """
        candidates = extract_jd_candidates(jd_text, url)
        assert len(candidates) == 1
        candidate = candidates[0]
        candidate.evidence_refs = [
            {"type": "page_text", "url": url, "content_hash": "abc123"}
        ]

        evidence = [
            PageEvidence(
                evidence_type="page_text",
                url=url,
                content_hash="abc123",
            )
        ]
        verified = verify_evidence(candidates, evidence)
        assert len(verified) == 1
        verified_candidate = verified[0]

        idem_key = build_candidate_idempotency_key(
            company=verified_candidate.company_name or "",
            title=verified_candidate.title or "",
            location=verified_candidate.locations[0] if verified_candidate.locations else "",
            apply_url=verified_candidate.apply_url or "",
            evidence_hash="abc123",
        )
        assert len(idem_key) == 64

        triage2 = triage_link(url)
        candidates2 = extract_jd_candidates(jd_text, url)
        candidates2[0].evidence_refs = [
            {"type": "page_text", "url": url, "content_hash": "abc123"}
        ]
        verified2 = verify_evidence(candidates2, evidence)
        idem_key2 = build_candidate_idempotency_key(
            company=verified2[0].company_name or "",
            title=verified2[0].title or "",
            location=verified2[0].locations[0] if verified2[0].locations else "",
            apply_url=verified2[0].apply_url or "",
            evidence_hash="abc123",
        )
        assert triage.site_type == triage2.site_type
        assert candidates[0].title == candidates2[0].title
        assert verified[0].title == verified2[0].title
        assert idem_key == idem_key2
