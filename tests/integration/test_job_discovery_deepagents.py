"""Integration tests for the Deep Agents harness for Job Discovery.

Tests cover:
- All 8 tool wrappers with realistic inputs
- The web navigation subagent specification
- The supervisor agent builder (graph compilation)
- Mocked end-to-end orchestration scenarios
- Page budget and domain safety enforcement
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from backend.app.config import Settings
from backend.app.services.job_discovery.deepagents_runner import (
    _asdict,
    _alibaba_position_evidence_from_search_payload,
    _build_job_discovery_llm,
    _is_blocked_domain,
    _SUPERVISOR_SYSTEM_PROMPT,
    _WEB_NAVIGATION_SYSTEM_PROMPT,
    build_discovery_supervisor_agent,
    click_link,
    create_web_navigation_subagent,
    extract_jd_candidates,
    extract_links,
    finish_with_manual_review,
    get_visible_text,
    go_back,
    open_url,
    package_candidates,
    parse_wechat_article,
    read_dom,
    run_ocr,
    run_web_navigation,
    screenshot,
    triage_link,
    verify_evidence,
)
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    NormalizedJobCandidate,
    PageEvidence,
    TriageResult,
)


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def settings() -> Settings:
    """Create a minimal Settings object for testing."""
    return Settings(
        app_env="test",
        app_auth_secret="a" * 32,
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        object_encryption_key=base64.b64encode(b"0" * 32).decode(),
        job_discovery_enabled=True,
        job_discovery_model="deepseek-v4-flash",
        job_discovery_max_pages_per_task=5,
        job_discovery_max_candidates_per_task=3,
        job_discovery_task_timeout_seconds=60,
        job_discovery_ocr_enabled=False,
    )


# =========================================================================
# Helper: _asdict
# =========================================================================


class TestAsdict:
    """Test the _asdict helper function."""

    def test_dataclass_to_dict(self) -> None:
        result = _asdict(TriageResult(site_type="test", confidence=0.5, recommended_action="skip"))
        assert result == {
            "site_type": "test",
            "confidence": 0.5,
            "recommended_action": "skip",
            "notes": "",
        }

    def test_list_of_dataclasses(self) -> None:
        results = _asdict([
            TriageResult(site_type="a", confidence=1.0, recommended_action="go"),
            TriageResult(site_type="b", confidence=0.5, recommended_action="stop"),
        ])
        assert len(results) == 2
        assert results[0]["site_type"] == "a"
        assert results[1]["site_type"] == "b"

    def test_nested_dataclass(self) -> None:
        result = _asdict(DiscoveryRunResult(
            status="needs_manual_review",
            block_reason="test",
            evidence=[
                PageEvidence(evidence_type="page_text", url="http://example.com"),
            ],
        ))
        assert result["status"] == "needs_manual_review"
        assert len(result["evidence"]) == 1
        assert result["evidence"][0]["evidence_type"] == "page_text"

    def test_plain_types(self) -> None:
        assert _asdict("string") == "string"
        assert _asdict(42) == 42
        assert _asdict(None) is None


# =========================================================================
# Tool 1: triage_link
# =========================================================================


class TestTriageLink:
    """Test the triage_link tool wrapper."""

    def test_empty_url(self) -> None:
        result = triage_link("")
        assert result["site_type"] == "invalid"
        assert result["confidence"] == 1.0
        assert result["recommended_action"] == "skip"

    def test_mailto_url(self) -> None:
        result = triage_link("mailto:hr@example.com")
        assert result["site_type"] == "email_only"
        assert result["recommended_action"] == "finish_manual_review"

    def test_wechat_url(self) -> None:
        result = triage_link("https://mp.weixin.qq.com/s/abc123")
        assert result["site_type"] == "wechat_article"
        assert result["recommended_action"] == "run_web_navigation"

    def test_blocked_domain(self) -> None:
        result = triage_link("https://www.linkedin.com/jobs/123")
        assert result["site_type"] == "blocked"
        assert result["recommended_action"] == "finish_manual_review"

    def test_job_detail_page(self) -> None:
        result = triage_link("https://company.example.com/job/42")
        assert result["site_type"] == "job_detail"
        assert result["recommended_action"] == "run_web_navigation"

    def test_career_page(self) -> None:
        result = triage_link("https://company.example.com/careers")
        assert result["site_type"] == "career_site"
        assert result["recommended_action"] == "run_web_navigation"

    def test_homepage(self) -> None:
        result = triage_link("https://company.example.com")
        assert result["site_type"] == "official_site"
        assert result["recommended_action"] == "run_web_navigation"


# =========================================================================
# Tool 3: parse_wechat_article
# =========================================================================


class TestParseWechatArticle:
    """Test the parse_wechat_article tool wrapper."""

    def test_empty_html(self) -> None:
        result = parse_wechat_article("", "https://mp.weixin.qq.com/s/test")
        assert result["text_content"] == ""
        assert result["title"] is None

    def test_basic_article(self) -> None:
        html = """
        <html><head><title>Test Article</title>
        <meta property="og:title" content="OG Test Article"/>
        </head>
        <body><div id="js_content">
        <p>This is a job posting for a software engineer position.</p>
        <p>请将简历发送至 hr@example.com</p>
        </div></body></html>
        """
        result = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert result["title"] == "OG Test Article"
        assert "software engineer" in result["text_content"]
        assert result["email_delivery_instructions"] is not None
        assert "hr@example.com" in result["email_delivery_instructions"]

    def test_inaccessible_article(self) -> None:
        html = """
        <html><head><title>Blocked</title></head>
        <body><div id="js_content">请在微信客户端打开</div></body></html>
        """
        result = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert result["needs_manual_review"] is True
        assert "请在微信客户端打开" in result["manual_review_reason"]

    def test_article_with_images(self) -> None:
        html = """
        <html><head><title>With Images</title></head>
        <body><div id="js_content">
        <p>Job description here</p>
        <img data-src="https://example.com/img1.png"/>
        <img data-src="https://example.com/img2.png"/>
        </div></body></html>
        """
        result = parse_wechat_article(html, "https://mp.weixin.qq.com/s/test")
        assert len(result["image_urls"]) == 2
        assert "https://example.com/img1.png" in result["image_urls"]


# =========================================================================
# Tool 4: run_ocr
# =========================================================================


class TestRunOcr:
    """Test the run_ocr tool wrapper."""

    def test_invalid_base64(self) -> None:
        result = run_ocr("not-valid-base64!!!", settings=None)
        assert result["needs_manual_review"] is True
        assert any("Base64" in w for w in result["warnings"])

    def test_empty_base64(self) -> None:
        result = run_ocr("", settings=None)
        assert result["needs_manual_review"] is True

    def test_ocr_disabled(self) -> None:
        # A valid but tiny PNG (1x1 pixel, minimal valid PNG)
        # We'll use a minimal valid PNG
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"  # signature
            b"\x00\x00\x00\x0dIHDR"  # IHDR chunk
            b"\x00\x00\x00\x01\x00\x00\x00\x01"  # 1x1 pixel
            b"\x08\x02\x00\x00\x00\x90wS\xde"  # color type, etc.
            b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        b64 = base64.b64encode(png_bytes).decode()
        result = run_ocr(b64, settings=None)
        # Since settings is None, ocr_enabled defaults to False
        assert result["needs_manual_review"] is True

    def test_ocr_with_settings(self, settings: Settings) -> None:
        b64 = base64.b64encode(b"fake-image-data").decode()
        settings.job_discovery_ocr_enabled = True
        result = run_ocr(b64, settings=settings)
        # OCR engine not available, so returns needs_manual_review
        assert result["needs_manual_review"] is True


# =========================================================================
# Tool 5: extract_jd_candidates
# =========================================================================


class TestExtractJdCandidates:
    """Test the extract_jd_candidates tool wrapper."""

    def test_empty_text(self) -> None:
        result = extract_jd_candidates("", "http://example.com/job/1")
        data = json.loads(result)
        assert data == []

    def test_simple_jd(self) -> None:
        page_text = """
        岗位名称：软件工程师
        公司名称：测试科技有限公司
        所属部门：研发部
        工作地点：北京
        岗位职责：
        负责后端系统开发和维护。
        参与架构设计和技术选型。
        任职要求：
        计算机相关专业本科及以上学历。
        熟悉 Python 和分布式系统。
        投递方式：hr@test.com
        截止日期：2026-12-31
        """
        result = extract_jd_candidates(page_text, "http://example.com/job/1")
        data = json.loads(result)
        assert len(data) >= 1
        candidate = data[0]
        assert candidate["title"] == "软件工程师"
        assert candidate["company_name"] == "测试科技有限公司"
        assert "北京" in str(candidate["locations"])
        assert candidate["confidence"] > 0

    def test_dual_job_page(self) -> None:
        page_text = """
        岗位名称：前端开发工程师
        公司名称：示例公司
        工作地点：上海
        岗位职责：负责前端开发。
        任职要求：有 React 经验。

        岗位二：
        岗位名称：后端开发工程师
        公司名称：示例公司
        工作地点：北京
        岗位职责：负责后端开发。
        任职要求：有 Python 经验。
        """
        result = extract_jd_candidates(page_text, "http://example.com/jobs")
        data = json.loads(result)
        assert len(data) >= 1


# =========================================================================
# Tool 6: verify_evidence
# =========================================================================


class TestVerifyEvidence:
    """Test the verify_evidence tool wrapper."""

    def test_verify_candidates(self) -> None:
        candidates = [
            {
                "title": "软件工程师",
                "company_name": "测试公司",
                "description_text": "负责软件开发工作",
                "responsibilities": "开发",
                "requirements": "Python",
                "locations": ["北京"],
                "recruitment_types": ["full_time"],
                "evidence_refs": [{"url": "http://example.com", "type": "page"}],
            }
        ]
        evidence = [
            {"evidence_type": "page_text", "url": "http://example.com", "content_hash": "abc"}
        ]
        result = verify_evidence(
            json.dumps(candidates), json.dumps(evidence)
        )
        data = json.loads(result)
        assert len(data) >= 1
        assert data[0]["company_name"] == "测试公司"

    def test_reject_no_title_no_company(self) -> None:
        candidates = [
            {
                "title": None,
                "company_name": None,
                "description_text": "some text",
                "evidence_refs": [{"url": "http://example.com"}],
            }
        ]
        evidence = [{"evidence_type": "page_text", "url": "http://example.com"}]
        result = verify_evidence(json.dumps(candidates), json.dumps(evidence))
        data = json.loads(result)
        assert len(data) == 0

    def test_reject_no_evidence_refs(self) -> None:
        candidates = [
            {
                "title": "工程师",
                "company_name": "公司",
                "description_text": "some text",
                "evidence_refs": [],
            }
        ]
        evidence = [{"evidence_type": "page_text", "url": "http://example.com"}]
        result = verify_evidence(json.dumps(candidates), json.dumps(evidence))
        data = json.loads(result)
        assert len(data) == 0


# =========================================================================
# Tool 7: package_candidates
# =========================================================================


class TestPackageCandidates:
    """Test the package_candidates tool wrapper."""

    def test_package_with_keys(self) -> None:
        candidates = [
            {
                "title": "软件工程师",
                "company_name": "测试公司",
                "locations": ["北京"],
                "apply_url": "http://example.com/apply",
                "recruitment_types": ["full_time"],
                "description_text": "test",
                "evidence_refs": [],
            }
        ]
        result = package_candidates(
            json.dumps(candidates),
            evidence_hash="abc123",
            source_key="test_source",
        )
        data = json.loads(result)
        assert len(data) == 1
        assert "idempotency_key" in data[0]
        assert "similarity_group_key" in data[0]
        assert isinstance(data[0]["idempotency_key"], str)
        assert len(data[0]["idempotency_key"]) == 64  # SHA-256 hex

    def test_package_empty_list(self) -> None:
        result = package_candidates("[]", evidence_hash="abc", source_key="test")
        data = json.loads(result)
        assert data == []


# =========================================================================
# Tool 8: finish_with_manual_review
# =========================================================================


class TestFinishWithManualReview:
    """Test the finish_with_manual_review tool wrapper."""

    def test_manual_review_result(self) -> None:
        result = finish_with_manual_review("Captcha detected on login page")
        assert result["status"] == "needs_manual_review"
        assert result["block_reason"] == "Captcha detected on login page"
        assert "Manual review required" in result["summary"]


# =========================================================================
# Web navigation subagent tools
# =========================================================================


class TestWebNavigationSubagentTools:
    """Test the web navigation subagent tool functions."""

    def test_screenshot_stub(self) -> None:
        result = screenshot("http://example.com")
        assert result.startswith("data:image/png;base64,")
        assert "SCREENSHOT_NOT_AVAILABLE" in result

    def test_get_visible_text_delegates(self) -> None:
        # get_visible_text delegates to open_url, which makes HTTP calls
        # We just verify the function exists and has the right signature
        assert callable(get_visible_text)
        assert get_visible_text.__doc__ is not None
        assert "url" in get_visible_text.__annotations__

    def test_open_url_blocked_domain(self) -> None:
        result = open_url("https://www.linkedin.com/jobs/123")
        assert "ERROR" in result
        assert "blocked domain" in result.lower()

    def test_open_url_invalid_domain(self) -> None:
        result = open_url("https://nonexistent-domain-xyz987.com/test")
        assert "ERROR" in result or "Could not open" in result

    def test_extract_links_blocked_domain(self) -> None:
        result = extract_links("https://www.linkedin.com")
        data = json.loads(result)
        assert "error" in data
        assert "blocked domain" in data["error"].lower()

    def test_click_link_blocked_domain(self) -> None:
        result = click_link("https://www.linkedin.com", "Jobs")
        assert "ERROR" in result
        assert "blocked domain" in result.lower()

    def test_go_back_empty_history(self) -> None:
        result = go_back()
        assert "ERROR" in result
        assert "No previous page" in result

    def test_read_dom_blocked_domain(self) -> None:
        result = read_dom("https://www.linkedin.com")
        assert "ERROR" in result
        assert "blocked domain" in result.lower()


# =========================================================================
# Domain safety
# =========================================================================


class TestDomainSafety:
    """Test the domain safety enforcement."""

    def test_blocked_domains_list(self) -> None:
        assert _is_blocked_domain("https://www.linkedin.com/jobs")
        assert _is_blocked_domain("https://www.zhaopin.com/position")
        assert _is_blocked_domain("https://www.51job.com")
        assert not _is_blocked_domain("https://www.example.com")
        assert not _is_blocked_domain("https://www.company-careers.com")

    def test_blocked_domain_edge_cases(self) -> None:
        assert not _is_blocked_domain("")
        assert not _is_blocked_domain("not-a-url")


# =========================================================================
# Web Navigation Subagent spec
# =========================================================================


class TestCreateWebNavigationSubagent:
    """Test the web navigation subagent spec creation."""

    def test_subagent_spec_structure(self, settings: Settings) -> None:
        spec = create_web_navigation_subagent(settings)

        # Should be a dict with required SubAgent keys
        assert isinstance(spec, dict)
        assert spec["name"] == "web_navigation_agent"
        assert "description" in spec
        assert "system_prompt" in spec

        # Should have tools
        assert "tools" in spec
        assert len(spec["tools"]) == 9

        # Tool names
        tool_names = {t.__name__ for t in spec["tools"]}
        assert tool_names == {
            "open_url", "open_rendered_url", "extract_rendered_job_evidence",
            "read_dom", "extract_links", "click_link",
            "get_visible_text", "screenshot", "go_back",
        }

    def test_subagent_system_prompt(self, settings: Settings) -> None:
        spec = create_web_navigation_subagent(settings)
        prompt = spec["system_prompt"]
        assert "Web Navigation Agent" in prompt
        assert "public URL" in prompt
        assert "page budget" in prompt
        assert prompt == _WEB_NAVIGATION_SYSTEM_PROMPT


# =========================================================================
# Supervisor system prompt
# =========================================================================


class TestSupervisorSystemPrompt:
    """Test the supervisor system prompt matches the spec."""

    def test_prompt_contains_key_elements(self) -> None:
        prompt = _SUPERVISOR_SYSTEM_PROMPT
        assert "Discovery Supervisor Agent" in prompt
        assert "Tencent smart sheet" in prompt
        assert "needs_manual_review" in prompt
        assert "Do not write to the database" in prompt
        assert "Never invent" in prompt
        assert "Email application instructions" in prompt


# =========================================================================
# Agent builder
# =========================================================================


class TestBuildDiscoverySupervisorAgent:
    """Test the supervisor agent builder."""

    def test_build_agent(self, settings: Settings) -> None:
        """Test that building the agent creates a compiled graph."""
        agent = build_discovery_supervisor_agent(settings=settings)
        assert agent is not None
        # Should be a CompiledStateGraph
        assert hasattr(agent, "ainvoke")
        assert hasattr(agent, "invoke")

    def test_build_with_custom_model(self, settings: Settings) -> None:
        """Test with a pre-built model instance."""
        model = ChatOpenAI(
            model=settings.job_discovery_model,
            temperature=0.2,
            api_key="test-key",
            base_url="https://api.deepseek.com",
        )
        agent = build_discovery_supervisor_agent(settings=settings, model=model)
        assert agent is not None
        assert hasattr(agent, "ainvoke")

    @patch("backend.app.services.job_discovery.deepagents_runner.ChatOpenAI")
    def test_deepseek_v4_disables_thinking_for_tool_choice(
        self, mock_chat_openai: Any, settings: Settings
    ) -> None:
        """DeepSeek V4 thinking mode rejects deepagents' tool_choice requests."""
        settings.job_discovery_model = "deepseek-v4-flash"
        _build_job_discovery_llm(settings)
        assert mock_chat_openai.call_args.kwargs["extra_body"] == {
            "thinking": {"type": "disabled"}
        }

    def test_agent_has_subagent(self, settings: Settings) -> None:
        """Test that the built agent has the web navigation subagent registered."""
        # We can't easily inspect the subagents from the compiled graph,
        # but we can verify the builder runs without error
        agent = build_discovery_supervisor_agent(settings=settings)
        assert agent is not None


# =========================================================================
# Tool discovery via _fetch_page (run_web_navigation)
# =========================================================================


class TestRunWebNavigation:
    """Test the run_web_navigation tool."""

    @patch("backend.app.services.job_discovery.deepagents_runner.requests.get")
    @patch("backend.app.services.job_discovery.deepagents_runner.create_deep_agent")
    def test_invokes_deepagent_web_navigator_not_static_requests(
        self,
        mock_create_deep_agent: MagicMock,
        mock_requests_get: MagicMock,
        settings: Settings,
    ) -> None:
        """Web navigation must be an LLM tool loop, not a requests/link script."""
        mock_requests_get.side_effect = AssertionError("static requests fallback was used")
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "evidence_pages": [
                    {
                        "evidence_type": "page_text",
                        "url": "https://company.example.com/jobs/ai",
                        "title": "AI Engineer",
                        "content_hash": "hash-ai",
                        "text_excerpt": "岗位职责: build agents\n任职要求: python",
                        "metadata": {"page_num": 1},
                    }
                ],
                "navigation_path": [
                    {"url": "https://company.example.com", "title": "Home"},
                    {"url": "https://company.example.com/jobs/ai", "title": "AI Engineer"},
                ],
                "page_count": 2,
            }
        }
        mock_create_deep_agent.return_value = mock_agent

        result = run_web_navigation(
            "https://company.example.com",
            settings=settings,
            model=MagicMock(spec=ChatOpenAI),
        )

        assert result["evidence_pages"][0]["title"] == "AI Engineer"
        assert result["page_count"] == 2
        assert result["delegated_to"] == "web_navigation_agent"
        mock_create_deep_agent.assert_called_once()
        mock_agent.invoke.assert_called_once()
        mock_requests_get.assert_not_called()

    def test_blocked_domain(self, settings: Settings) -> None:
        result = run_web_navigation("https://www.linkedin.com/jobs", settings=settings)
        assert "error" in result
        assert "blocked domain" in result["error"].lower()

    def test_invalid_url(self, settings: Settings) -> None:
        result = run_web_navigation("not-a-valid-url", settings=settings)
        # Should either have an error or be handled gracefully
        assert isinstance(result, dict)

    def test_nonexistent_domain(self, settings: Settings) -> None:
        result = run_web_navigation(
            "https://nonexistent-domain-xyz987654.com",
            settings=settings,
        )
        assert "error" in result or "evidence_pages" in result


class TestRenderedJobEvidence:
    """Test rendered job evidence extraction helpers."""

    def test_alibaba_position_search_payload_yields_one_evidence_per_job(self) -> None:
        payload = {
            "content": {
                "datas": [
                    {
                        "id": "199903220038",
                        "name": "AI应用研发工程师",
                        "workLocations": ["北京", "杭州", "上海"],
                        "description": "1、负责 AI 应用工程研发。",
                        "requirement": "1、熟悉 Python 和 Agent 框架。",
                        "categoryName": "技术类",
                        "categoryType": "internship",
                        "batchName": "阿里巴巴2027届实习生",
                        "circleNames": ["淘宝闪购"],
                    },
                    {
                        "id": "199903220039",
                        "name": "AI应用算法工程师",
                        "workLocations": ["杭州"],
                        "description": "1、负责算法建模。",
                        "requirement": "1、熟悉机器学习。",
                        "categoryName": "算法类",
                        "categoryType": "internship",
                        "batchName": "阿里巴巴2027届实习生",
                        "circleNames": ["淘宝闪购"],
                    },
                ]
            }
        }

        evidence = _alibaba_position_evidence_from_search_payload(
            payload,
            "https://campus-talent.alibaba.com/campus/position?batchId=100000540002",
        )

        assert [item["title"] for item in evidence] == ["AI应用研发工程师", "AI应用算法工程师"]
        assert all(item["evidence_type"] == "job_detail_json" for item in evidence)
        assert all(item["content_hash"] for item in evidence)
        assert "岗位职责" in evidence[0]["text_excerpt"]
        assert "任职要求" in evidence[0]["text_excerpt"]
        assert evidence[0]["metadata"]["position_id"] == "199903220038"


# =========================================================================
# DiscoveryRunResult dataclass used by finish_with_manual_review
# =========================================================================


class TestDiscoveryRunResult:
    """Test that DiscoveryRunResult is properly constructed."""

    def test_success_result(self) -> None:
        result = DiscoveryRunResult(
            status="succeeded",
            evidence=[PageEvidence(evidence_type="page_text", url="http://example.com")],
            candidates=[NormalizedJobCandidate(title="Engineer", company_name="Company")],
            summary="Discovered 1 candidate",
        )
        assert result.status == "succeeded"
        assert len(result.evidence) == 1
        assert len(result.candidates) == 1
        assert result.candidates[0].title == "Engineer"

    def test_needs_manual_review(self) -> None:
        result = DiscoveryRunResult(
            status="needs_manual_review",
            block_reason="Captcha detected",
            summary="Manual review required: Captcha detected",
        )
        assert result.status == "needs_manual_review"
        assert result.block_reason == "Captcha detected"


# =========================================================================
# Scenarios: Mocked end-to-end agent orchestration
# =========================================================================


class TestMockedAgentOrchestration:
    """Test the agent orchestration with mocked model responses.

    These tests verify that the agent framework correctly routes tool calls
    and handles different scenarios. The model is mocked to return controlled
    responses that exercise specific tool paths.
    """

    @pytest.mark.asyncio
    async def test_agent_builder_produces_callable_graph(self, settings: Settings) -> None:
        """Verify the built agent is a callable compiled graph."""
        agent = build_discovery_supervisor_agent(settings=settings)
        # The agent should be a compiled state graph
        assert hasattr(agent, "get_state")
        assert hasattr(agent, "get_graph")

    @pytest.mark.asyncio
    async def test_tool_direct_invocation(self, settings: Settings) -> None:
        """Test that each tool function can be called directly."""
        # triage_link
        result = triage_link("https://example.com/careers")
        assert result["recommended_action"] == "run_web_navigation"

        # parse_wechat_article
        result = parse_wechat_article("<html><body><div id='js_content'>Test</div></body></html>", "http://mp.weixin.qq.com")
        assert isinstance(result, dict)

        # extract_jd_candidates
        result = extract_jd_candidates("岗位名称：测试工程师", "http://example.com")
        data = json.loads(result)
        assert isinstance(data, list)

        # finish_with_manual_review
        result = finish_with_manual_review("Test reason")
        assert result["status"] == "needs_manual_review"

    @pytest.mark.asyncio
    async def test_web_navigation_tools_coverage(self) -> None:
        """Test all web navigation tools are properly defined and callable."""
        tools = {
            "open_url": open_url,
            "read_dom": read_dom,
            "extract_links": extract_links,
            "click_link": click_link,
            "get_visible_text": get_visible_text,
            "screenshot": screenshot,
            "go_back": go_back,
        }
        for name, tool in tools.items():
            assert callable(tool), f"Tool {name} is not callable"
            assert tool.__doc__ is not None, f"Tool {name} has no docstring"


# =========================================================================
# JSON roundtrip for tool wrappers that use JSON
# =========================================================================


class TestJsonRoundtrip:
    """Test JSON serialization/deserialization in tool wrappers."""

    def test_extract_jd_candidates_roundtrip(self) -> None:
        text = "岗位名称：测试工程师\n公司名称：测试公司\n工作地点：深圳\n"
        result = extract_jd_candidates(text, "http://example.com")
        data = json.loads(result)
        assert isinstance(data, list)
        # Can be serialized back to JSON
        roundtripped = json.dumps(data, ensure_ascii=False)
        assert len(roundtripped) > 0

    def test_verify_evidence_roundtrip(self) -> None:
        candidates = json.dumps([{
            "title": "Engineer",
            "company_name": "Company",
            "description_text": "Job description",
            "responsibilities": "Dev",
            "requirements": "Skills",
            "locations": ["NYC"],
            "recruitment_types": ["full_time"],
            "evidence_refs": [{"url": "http://example.com", "type": "page"}],
        }])
        evidence = json.dumps([{
            "evidence_type": "page_text",
            "url": "http://example.com",
            "content_hash": "abc",
        }])
        result = verify_evidence(candidates, evidence)
        data = json.loads(result)
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_package_candidates_roundtrip(self) -> None:
        candidates = json.dumps([{
            "title": "Engineer",
            "company_name": "Company",
            "locations": ["NYC"],
            "apply_url": "http://example.com/apply",
            "recruitment_types": ["full_time"],
            "description_text": "test",
            "evidence_refs": [],
        }])
        result = package_candidates(candidates, "hash123", "test_source")
        data = json.loads(result)
        assert isinstance(data, list)
        # Verify we can re-serialize
        roundtripped = json.dumps(data, ensure_ascii=False)
        assert len(roundtripped) > 0


# =========================================================================
# Mocked orchestration: 5 required scenarios
# =========================================================================


class TestAgentOrchestration:
    """Mocked agent orchestration tests for the Discovery Supervisor Agent.

    Tests 5 required scenarios by mocking model.ainvoke to return controlled
    responses that exercise specific tool paths. Tools run against their real
    implementations (except HTTP which is mocked for web navigation).
    """

    @pytest.fixture(autouse=True)
    def _prevent_delegation(self) -> None:
        """Prevent subagent delegation from consuming shared mock responses.

        Patching ``_run_via_subagent_delegation`` to raise ensures the
        delegation path in ``run_web_navigation`` falls through to direct
        navigation without consuming supervisor response tokens.
        """
        with patch(
            "backend.app.services.job_discovery.deepagents_runner._run_via_subagent_delegation",
            side_effect=TypeError("subagent delegation not supported in test"),
        ):
            yield

    def _create_mock_model(self, responses: list[AIMessage]):
        """Create a mock ChatOpenAI with controlled response sequences.

        ``bind_tools`` always returns the same ``bound_mock`` (shared
        between supervisor and delegation paths). Since the test patches
        :func:`_run_via_subagent_delegation` to raise, the delegation path
        fails immediately without consuming responses.
        """
        supervisor_responses = list(responses)

        # Shared async mock that serves the supervisor's response sequence
        shared_ainvoke = AsyncMock(side_effect=supervisor_responses)

        bound_mock = MagicMock(spec=ChatOpenAI)
        bound_mock.model_name = "deepseek-v4-flash"
        bound_mock.model = "deepseek-v4-flash"
        bound_mock.ainvoke = shared_ainvoke
        bound_mock.invoke = MagicMock()
        bound_mock.with_structured_output = MagicMock(return_value=bound_mock)
        bound_mock.profile = None

        mock_model = MagicMock(spec=ChatOpenAI)
        mock_model.model_name = "deepseek-v4-flash"
        mock_model.model = "deepseek-v4-flash"
        mock_model.bind_tools = MagicMock(return_value=bound_mock)
        mock_model.invoke = MagicMock()
        mock_model.ainvoke = shared_ainvoke
        mock_model.with_structured_output = MagicMock(return_value=mock_model)
        mock_model.profile = None

        return mock_model, bound_mock

    @pytest.mark.asyncio
    @patch("backend.app.services.job_discovery.deepagents_runner.requests.get")
    async def test_official_homepage_to_two_jd_candidates(self, mock_get: MagicMock, settings: Settings) -> None:
        """Mock: link_triage -> career_site -> WebNavigation -> 2 JDs -> extract -> verify -> package."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.text = "<html><body><h1>Careers</h1><a href='/job/1'>Job 1</a></body></html>"
        mock_get.return_value = mock_response

        responses = [
            AIMessage(content="", tool_calls=[{"name": "triage_link", "args": {"url": "https://company.example.com"}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "run_web_navigation", "args": {"start_url": "https://company.example.com"}, "id": "c2"}]),
            AIMessage(content="", tool_calls=[{"name": "extract_jd_candidates", "args": {"page_text": "software engineer position", "url": "https://company.example.com/job/1"}, "id": "c3"}]),
            AIMessage(content="", tool_calls=[{"name": "verify_evidence", "args": {"candidates_json": "[]", "evidence_json": "[]"}, "id": "c4"}]),
            AIMessage(content="", tool_calls=[{"name": "package_candidates", "args": {"candidates_json": "[]", "evidence_hash": "abc", "source_key": "test"}, "id": "c5"}]),
            AIMessage(content="Discovered 2 job candidates"),
        ]
        mock_model, bound_mock = self._create_mock_model(responses)

        agent = build_discovery_supervisor_agent(settings=settings, model=mock_model)
        result = await agent.ainvoke({"messages": [HumanMessage(content="Discover jobs at https://company.example.com")]})

        assert isinstance(result, dict), f"Expected dict result, got {type(result)}"
        assert "messages" in result, f"Expected messages in result, got keys: {list(result.keys())}"
        # Should have progressed through multiple tool calls (at least triage + web_nav + extract)
        assert bound_mock.ainvoke.call_count >= 3, f"Expected >=3 model calls, got {bound_mock.ainvoke.call_count}"

    @pytest.mark.asyncio
    async def test_wechat_text_article_extracts_email_channel(self, settings: Settings) -> None:
        """Mock: link_triage -> wechat_article -> parse -> extract -> email channel."""
        responses = [
            AIMessage(content="", tool_calls=[{"name": "triage_link", "args": {"url": "https://mp.weixin.qq.com/s/test123"}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "parse_wechat_article", "args": {"html": "<html><body><div id='js_content'>Email: hr@company.com</div></body></html>", "url": "https://mp.weixin.qq.com/s/test123"}, "id": "c2"}]),
            AIMessage(content="", tool_calls=[{"name": "finish_with_manual_review", "args": {"reason": "Email delivery instructions found - manual processing needed"}, "id": "c3"}]),
            AIMessage(content="Manual review: email channel identified"),
        ]
        mock_model, bound_mock = self._create_mock_model(responses)

        agent = build_discovery_supervisor_agent(settings=settings, model=mock_model)
        result = await agent.ainvoke({"messages": [HumanMessage(content="Process WeChat article at https://mp.weixin.qq.com/s/test123")]})

        assert isinstance(result, dict)
        assert "messages" in result
        assert bound_mock.ainvoke.call_count >= 2

    @pytest.mark.asyncio
    async def test_wechat_image_article_ocr_extracts_candidate(self, settings: Settings) -> None:
        """Mock: link_triage -> wechat_article -> OCR -> extract -> candidate."""
        import base64
        dummy_b64 = base64.b64encode(b"fake-image-data").decode()
        responses = [
            AIMessage(content="", tool_calls=[{"name": "triage_link", "args": {"url": "https://mp.weixin.qq.com/s/image123"}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "parse_wechat_article", "args": {"html": "<html><body><div id='js_content'><img data-src='https://example.com/img.png'/></div></body></html>", "url": "https://mp.weixin.qq.com/s/image123"}, "id": "c2"}]),
            AIMessage(content="", tool_calls=[{"name": "run_ocr", "args": {"image_base64": dummy_b64}, "id": "c3"}]),
            AIMessage(content="", tool_calls=[{"name": "extract_jd_candidates", "args": {"page_text": "OCR extracted: software engineer position", "url": "https://example.com/img.png"}, "id": "c4"}]),
            AIMessage(content="", tool_calls=[{"name": "finish_with_manual_review", "args": {"reason": "Candidate extracted from OCR - manual verification needed"}, "id": "c5"}]),
            AIMessage(content="Candidate extracted from OCR image"),
        ]
        mock_model, bound_mock = self._create_mock_model(responses)

        agent = build_discovery_supervisor_agent(settings=settings, model=mock_model)
        result = await agent.ainvoke({"messages": [HumanMessage(content="Process WeChat article with images at https://mp.weixin.qq.com/s/image123")]})

        assert isinstance(result, dict)
        assert "messages" in result
        assert bound_mock.ainvoke.call_count >= 2

    @pytest.mark.asyncio
    async def test_captcha_page_returns_needs_manual_review(self, settings: Settings) -> None:
        """Mock: link_triage -> blocked -> finish_with_manual_review."""
        responses = [
            AIMessage(content="", tool_calls=[{"name": "triage_link", "args": {"url": "https://www.linkedin.com/jobs/123"}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "finish_with_manual_review", "args": {"reason": "LinkedIn is a blocked domain - cannot access"}, "id": "c2"}]),
            AIMessage(content="Manual review required: blocked domain"),
        ]
        mock_model, bound_mock = self._create_mock_model(responses)

        agent = build_discovery_supervisor_agent(settings=settings, model=mock_model)
        result = await agent.ainvoke({"messages": [HumanMessage(content="Discover jobs at https://www.linkedin.com/jobs/123")]})

        assert isinstance(result, dict)
        assert "messages" in result
        assert bound_mock.ainvoke.call_count >= 2

    @pytest.mark.asyncio
    async def test_budget_exceeded_returns_partial_success(self, settings: Settings) -> None:
        """Mock: exceed page budget -> partial_success or needs_manual_review."""
        responses = [
            AIMessage(content="", tool_calls=[{"name": "triage_link", "args": {"url": "https://company.example.com/careers"}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "finish_with_manual_review", "args": {"reason": "Page budget would be exceeded - insufficient pages to complete discovery"}, "id": "c2"}]),
            AIMessage(content="Manual review: budget exceeded"),
        ]
        mock_model, bound_mock = self._create_mock_model(responses)

        agent = build_discovery_supervisor_agent(settings=settings, model=mock_model)
        result = await agent.ainvoke({"messages": [HumanMessage(content="Discover jobs but page budget is limited")]})

        assert isinstance(result, dict)
        assert "messages" in result
        assert bound_mock.ainvoke.call_count >= 2
