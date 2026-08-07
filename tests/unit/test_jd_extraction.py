"""Unit tests for the retained deterministic JD extraction tool.

``extract_jd_candidates`` is the pure-function extraction primitive the PEV
``job-discovery`` Skill reuses (see ``career_skills.job_discovery``). It has no
LLM/DB/network dependency, so every branch is exercised with deterministic
Chinese/English fixture text.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services.job_discovery.tools import jd_extraction
from backend.app.services.job_discovery.tools.jd_extraction import (
    _card_meta_cities,
    _detect_recruitment_types,
    _extract_apply_method,
    _extract_company,
    _extract_deadline,
    _extract_department,
    _extract_from_unstructured_text,
    _extract_locations,
    _extract_referral_code,
    _extract_section,
    _extract_title,
    _fuzzy_extract_title,
    _split_multi_job_page,
    extract_jd_candidates,
)
from backend.app.services.job_discovery.schemas import (
    NormalizedJobCandidate,
    RecruitmentScope,
    StrategyRecord,
)


# ---------------------------------------------------------------------------
# schemas.py: StrategyRecord.from_orm + RecruitmentScope.__post_init__
# ---------------------------------------------------------------------------


def test_strategy_record_from_orm_maps_all_fields() -> None:
    """``from_orm`` copies every field, coercing a falsy description to ``''``."""
    orm = SimpleNamespace(
        id="strat-1",
        url_pattern="*.example.com",
        site_type="career_site",
        description=None,
        priority=2,
        adapter="ExampleAdapter",
        plan_yaml="steps: []",
        status="active",
        success_count=7,
    )
    record = StrategyRecord.from_orm(orm)
    assert record.id == "strat-1"
    assert record.url_pattern == "*.example.com"
    assert record.description == ""  # None coerced to empty string
    assert record.priority == 2
    assert record.adapter == "ExampleAdapter"
    assert record.plan_yaml == "steps: []"
    assert record.status == "active"
    assert record.success_count == 7


def test_recruitment_scope_social_clears_graduation_year() -> None:
    scope = RecruitmentScope(recruitment_type="social", graduation_year=2027)
    assert scope.graduation_year is None


def test_recruitment_scope_campus_requires_graduation_year() -> None:
    with pytest.raises(ValueError, match="graduation_year is required"):
        RecruitmentScope(recruitment_type="campus", graduation_year=None)


def test_recruitment_scope_internship_keeps_graduation_year() -> None:
    scope = RecruitmentScope(recruitment_type="internship", graduation_year=2028)
    assert scope.graduation_year == 2028


# ---------------------------------------------------------------------------
# _split_multi_job_page
# ---------------------------------------------------------------------------


def test_split_multi_job_page_whitespace_returns_single_segment() -> None:
    assert _split_multi_job_page("   \n  ") == ["   \n  "]


def test_split_multi_job_page_numbered_separator() -> None:
    text = "岗位名称: AI工程师\n岗位职责: 开发\n岗位二：\n岗位名称: 后端工程师"
    segments = _split_multi_job_page(text)
    assert len(segments) == 2
    assert "AI工程师" in segments[0]
    assert "后端工程师" in segments[1]


def test_split_multi_job_page_repeated_title_headers() -> None:
    text = "岗位名称: AI工程师\n职责: x\n岗位名称: 后端工程师\n职责: y"
    segments = _split_multi_job_page(text)
    assert len(segments) == 2
    assert segments[0].startswith("岗位名称: AI工程师")
    assert segments[1].startswith("岗位名称: 后端工程师")


def test_split_multi_job_page_single_job_returns_self() -> None:
    text = "岗位名称: AI工程师\n岗位职责: 开发"
    assert _split_multi_job_page(text) == [text]


def test_split_multi_job_page_feishu_card_list() -> None:
    """Feishu card listings split per card with injected title/location headers."""
    text = (
        "座舱Agent Harness算法工程师\n"
        "北京、上海校招正式技术提前批职位 ID：A33756\n"
        "* 探索 agent harness\n"
        "Agent编排平台工程师\n"
        "上海校招正式职位 ID：A33757\n"
        "* 调度与编排\n"
        "多模态研究实习生\n"
        "深圳、杭州实习职位 ID：A33758\n"
        "* 多模态理解\n"
    )
    segments = _split_multi_job_page(text)
    assert len(segments) == 3
    assert segments[0].startswith(
        "职位名称：座舱Agent Harness算法工程师\n工作地点：北京、上海"
    )
    assert "A33756" in segments[0]
    assert "探索 agent harness" in segments[0]
    assert segments[1].startswith("职位名称：Agent编排平台工程师\n工作地点：上海")
    assert "A33757" in segments[1]
    assert segments[2].startswith("职位名称：多模态研究实习生\n工作地点：深圳、杭州")
    assert "A33758" in segments[2]


def test_split_multi_job_page_single_card_normalizes() -> None:
    """A lone card still becomes a single normalized segment."""
    text = "大模型Agent研发工程师\n北京校招职位 ID：A100\n* 研究\n"
    segments = _split_multi_job_page(text)
    assert len(segments) == 1
    assert segments[0].startswith("职位名称：大模型Agent研发工程师\n工作地点：北京")


def test_split_multi_job_page_card_without_city_lead_skips_location_header() -> None:
    """A meta line without a city lead (e.g. digit-led 2027届) omits 工作地点."""
    text = "Agent调度工程师\n2027届校招正式职位 ID：A200\n* 调度\n"
    segments = _split_multi_job_page(text)
    assert len(segments) == 1
    assert segments[0] == (
        "职位名称：Agent调度工程师\n"
        "Agent调度工程师\n"
        "2027届校招正式职位 ID：A200\n"
        "* 调度"
    )


def test_split_multi_job_page_card_title_with_trailing_detail() -> None:
    """Titles trailing detail after the role token (（AI平台）/-NOMI) still match."""
    text = (
        "提前批-AI产品经理（AI平台）\n"
        "上海校招正式产品 - 产品经理本科及以上2027届校园招聘-技术提前批职位 ID：A400\n"
        "* 负责产品规划\n"
    )
    segments = _split_multi_job_page(text)
    assert len(segments) == 1
    assert segments[0].startswith("职位名称：提前批-AI产品经理（AI平台）\n工作地点：上海")


def test_split_multi_job_page_chrome_above_meta_is_not_a_title() -> None:
    """A chrome line (推荐投递) above a meta must never become a card title."""
    text = (
        "提前批-Agent开发工程师-NOMI\n"
        "北京、上海校招正式数字技术 - 算法本科及以上2027届职位 ID：A500\n"
        "* 负责座舱\n"
        "推荐投递\n"
        "上海、合肥校招正式数字技术 - 软件研发硕士及以上2027届职位 ID：A501\n"
        "提前批-AI软件研发工程师（产研&企业治理领域）\n"
        "上海、合肥校招正式数字技术 - 软件研发硕士及以上2027届职位 ID：A502\n"
        "* 负责研发\n"
    )
    segments = _split_multi_job_page(text)
    assert len(segments) == 2
    assert [s.split("\n", 1)[0] for s in segments] == [
        "职位名称：提前批-Agent开发工程师-NOMI",
        "职位名称：提前批-AI软件研发工程师（产研&企业治理领域）",
    ]
    candidates = extract_jd_candidates(text, "https://nio.jobs.feishu.cn/x")
    assert [c.title for c in candidates] == [
        "提前批-Agent开发工程师-NOMI",
        "提前批-AI软件研发工程师（产研&企业治理领域）",
    ]
    assert all("推荐投递" not in (c.title or "") for c in candidates)


def test_card_meta_cities_guard_rejects_non_city_lead() -> None:
    """The meta-line city read accepts real cities, rejects non-city leads."""
    assert _card_meta_cities("北京、上海校招正式技术提前批职位 ID：A1") == "北京、上海"
    assert _card_meta_cities("上海校招正式职位 ID：A2") == "上海"
    assert _card_meta_cities("北京市社招职位 ID：A3") == "北京市"
    # All-Chinese non-city lead: too long to be a lone city, no 、, no suffix.
    assert _card_meta_cities("本科及以上校招职位 ID：A4") is None
    # Digit-led lead fails the character class.
    assert _card_meta_cities("2027届校招职位 ID：A5") is None
    # No 校招/社招/实习 marker at all.
    assert _card_meta_cities("职位 ID：A6") is None


def test_card_meta_cities_reads_etc_city_count_format() -> None:
    """Feishu's ``武汉、合肥、上海等 4 个城市校招...`` lead reads as cities."""
    assert (
        _card_meta_cities(
            "武汉、合肥、上海等 4 个城市校招正式数字技术 - 软件研发硕士及以上2027届职位 ID：A7"
        )
        == "武汉、合肥、上海"
    )
    # The 等 marker alone must not smuggle a non-city lead past the guard.
    assert _card_meta_cities("本科及以上等校招职位 ID：A8") is None


# ---------------------------------------------------------------------------
# individual extractors
# ---------------------------------------------------------------------------


def test_extract_title_labeled() -> None:
    title, conf = _extract_title("岗位名称: AI应用开发工程师\n其它")
    assert title == "AI应用开发工程师"
    assert conf == pytest.approx(0.7)


def test_extract_title_none_when_missing() -> None:
    assert _extract_title("一段没有任何标题关键词的普通文本") == (None, 0.0)


def test_extract_department() -> None:
    assert _extract_department("所属部门: 技术中台\n") == "技术中台"
    assert _extract_department("一段没有任何关键词的普通文本") is None


def test_extract_section_with_next_header() -> None:
    text = "岗位职责:\n负责Agent应用开发与评测\n任职要求:\nPython熟练"
    section = _extract_section(text, jd_extraction._RESPONSIBILITIES_HEADERS)
    assert "Agent应用开发与评测" in section
    assert "任职要求" not in section


def test_extract_section_without_next_header() -> None:
    text = "岗位职责:\n负责Agent开发与评测"
    section = _extract_section(text, jd_extraction._RESPONSIBILITIES_HEADERS)
    assert "Agent开发" in section


def test_extract_section_missing_returns_empty() -> None:
    assert _extract_section("没有任何已知章节标题的文本", jd_extraction._RESPONSIBILITIES_HEADERS) == ""


def test_extract_locations_splits_delimiters() -> None:
    # ``re.split(r"[,;、/\s]{2,}", ...)`` needs 2+ consecutive delimiters;
    # comma+space delimits cleanly, the Chinese 、 alone does not.
    locs = _extract_locations("工作地点: 北京, 上海, 深圳")
    assert locs == ["北京", "上海", "深圳"]


def test_detect_recruitment_types_all_three() -> None:
    types = _detect_recruitment_types("实习/校招/社招均开放，full-time available")
    assert "internship" in types
    assert "campus_recruitment" in types
    assert "full_time" in types


def test_detect_recruitment_types_dedup() -> None:
    types = _detect_recruitment_types("实习生 intern 实习")
    assert types == ["internship"]


def test_extract_apply_method_email() -> None:
    method = _extract_apply_method("投递方式: 请发送简历至 resume@company.com")
    assert method == {"method": "email", "email": "resume@company.com", "gui_eligible": False}


def test_extract_apply_method_unknown() -> None:
    method = _extract_apply_method("申请方式: 点击下方链接在线申请")
    assert method == {"method": "unknown", "gui_eligible": True}


def test_extract_apply_method_missing() -> None:
    assert _extract_apply_method("一段完全无关的普通描述文本内容") is None


def test_extract_deadline() -> None:
    assert _extract_deadline("截止日期: 2026-12-31") == "2026-12-31"
    assert _extract_deadline("一段完全无关的普通描述文本内容") is None


def test_extract_referral_code() -> None:
    assert _extract_referral_code("内推码: ABC123") == "ABC123"
    assert _extract_referral_code("无内推码") is None


# ---------------------------------------------------------------------------
# _fuzzy_extract_title (5 patterns + cleanups)
# ---------------------------------------------------------------------------


def test_fuzzy_title_pattern1_recruit_prefix() -> None:
    # Non-greedy capture stops at the FIRST role token in the alternation;
    # "开发" appears before "工程师" in the string, so the title is
    # "AI应用开发" (not "AI应用开发工程师").
    assert _fuzzy_extract_title("我们正在招募AI应用开发工程师，欢迎投递") == "AI应用开发"


def test_fuzzy_title_pattern1b_wechat_separator() -> None:
    title = _fuzzy_extract_title("字节跳动丨2026春招招聘开启")
    assert "招聘" in title


def test_fuzzy_title_pattern2_position_label() -> None:
    # No 招聘/招募 prefix -> Pattern 2 fires on the bare 岗位 label.
    assert _fuzzy_extract_title("岗位: 大模型算法工程师") == "大模型算法工程师"


def test_fuzzy_title_pattern3_line_ending_with_post() -> None:
    assert _fuzzy_extract_title("Agent开发岗") == "Agent开发岗"


def test_fuzzy_title_pattern4_separator_cleaned() -> None:
    title = _fuzzy_extract_title("丨字节跳动招聘丨")
    assert title == "字节跳动招聘"


def test_fuzzy_title_pattern5_first_meaningful_line() -> None:
    title = _fuzzy_extract_title("2026春招实习招聘\n详情见正文")
    assert title is not None
    assert "招聘" in title


def test_fuzzy_title_pattern5_when_keyword_at_start() -> None:
    # When the recruitment keyword sits at index 0, Pattern 4 (which needs
    # >=2 chars before the keyword) fails and Pattern 5 fires instead,
    # exercising the prefix/suffix cleanup (原创/分享/! removal).
    assert _fuzzy_extract_title("招聘信息已发布") == "招聘信息已发布"


def test_fuzzy_title_returns_none_when_nothing_matches() -> None:
    # No recruitment keyword, no role token, no separator -> every pattern misses.
    assert _fuzzy_extract_title("这是一段完全普通的关于天气和风景的描述文字xyz") is None


# ---------------------------------------------------------------------------
# _extract_from_unstructured_text (direct unit test)
# ---------------------------------------------------------------------------


def test_extract_from_unstructured_text_short_returns_none() -> None:
    assert _extract_from_unstructured_text("短文本", "https://x.com") is None


def test_extract_from_unstructured_text_few_keywords_returns_none() -> None:
    text = "这是一段超过五十个字符长度的普通文本，但只包含招聘这一个关键词，其余全是无关内容" * 2
    assert _extract_from_unstructured_text(text, "https://x.com") is None


def test_extract_from_unstructured_text_builds_candidate() -> None:
    text = (
        "公司名称: 字节跳动\n"
        "我们正在招聘AI应用开发工程师，实习岗，工作地点: 北京。\n"
        "请投递简历至邮箱，内推码: ABC123\n"
        "截止日期: 2026-12-31\n"
    )
    candidate = _extract_from_unstructured_text(text, "https://x.com")
    assert candidate is not None
    assert candidate.company_name == "字节跳动"
    assert any("北京" in loc for loc in candidate.locations)
    assert candidate.referral_code.startswith("ABC123")
    assert candidate.deadline_text.startswith("2026-12-31")
    assert candidate.apply_url == "https://x.com"


def test_extract_from_unstructured_text_company_from_separator_first_line() -> None:
    """When no ``公司`` label is present, company is split from a ``丨`` title."""
    text = "字节跳动丨招聘AI工程师\n" + "我们正在招聘实习岗位 " * 8
    candidate = _extract_from_unstructured_text(text, "https://x.com")
    assert candidate is not None
    assert candidate.company_name == "字节跳动"


# ---------------------------------------------------------------------------
# extract_jd_candidates (public entry)
# ---------------------------------------------------------------------------


def test_extract_jd_candidates_empty_returns_empty() -> None:
    assert extract_jd_candidates("", "https://x.com") == []
    assert extract_jd_candidates("   \n  ", "https://x.com") == []


def test_extract_jd_candidates_structured_jd() -> None:
    text = (
        "岗位名称: AI应用开发工程师\n"
        "公司名称: 字节跳动\n"
        "所属部门: 技术中台\n"
        "岗位职责:\n负责Agent应用设计与开发\n"
        "任职要求:\n熟悉Python与LangChain\n"
        "工作地点: 北京\n"
        "投递方式: resume@bytedance.com\n"
        "截止日期: 2026-12-31\n"
        "内推码: BYTEDANCE2026\n"
    )
    candidates = extract_jd_candidates(text, "https://jobs.example.com/1")
    assert len(candidates) == 1
    c = candidates[0]
    assert c.title == "AI应用开发工程师"
    assert c.company_name == "字节跳动"
    assert c.department == "技术中台"
    assert "Agent" in c.responsibilities
    assert "Python" in c.requirements
    assert "北京" in c.locations
    assert c.recruitment_types == []  # text carries no 实习/校招/社招 keyword
    assert c.application_channel_json == {
        "method": "email",
        "email": "resume@bytedance.com",
        "gui_eligible": False,
    }
    assert c.deadline_text == "2026-12-31"
    assert c.referral_code == "BYTEDANCE2026"
    assert c.apply_url == "https://jobs.example.com/1"
    assert c.confidence > 0.0


def test_extract_jd_candidates_multi_job_page_dedups() -> None:
    text = (
        "岗位名称: AI工程师\n公司名称: A公司\n岗位职责: 开发\n"
        "岗位二：\n岗位名称: AI工程师\n公司名称: A公司\n岗位职责: 后端"
    )
    candidates = extract_jd_candidates(text, "https://x.com")
    # Second segment has identical title+company -> deduplicated.
    assert len(candidates) == 1
    assert candidates[0].title == "AI工程师"


def test_extract_jd_candidates_unstructured_fallback_uses_full_segment() -> None:
    """No structured sections and no labeled title -> fuzzy title + full-text desc."""
    text = (
        "字节跳动丨2026春招AI应用开发招聘\n"
        "我们正在招募AI应用开发工程师，要求熟悉Agent框架与Python，"
        "工作地点北京上海，实习岗，请投递简历。"
    )
    candidates = extract_jd_candidates(text, "https://x.com")
    assert len(candidates) == 1
    c = candidates[0]
    assert c.title is not None
    assert "招聘" in c.title or "Agent" in c.description_text
    assert c.confidence >= 0.35  # unstructured fallback floor
    assert any("No job title" in w or "No responsibilities" in w for w in c.normalization_warnings)


def test_extract_jd_candidates_title_without_sections_falls_back_to_segment_desc() -> None:
    """Title found but no resp/req sections -> description_text uses trimmed segment."""
    text = "岗位名称: 算法研究员\n一段较短但没有标准章节标题的描述内容"
    candidates = extract_jd_candidates(text, "https://x.com")
    assert len(candidates) == 1
    c = candidates[0]
    assert c.title == "算法研究员"
    # No responsibilities/requirements and not unstructured-fallback (title exists),
    # so description_text takes the else branch (segment[:2000]).
    assert "算法研究员" in c.description_text
    assert any("No responsibilities" in w for w in c.normalization_warnings)


def test_extract_jd_candidates_no_title_emits_warning() -> None:
    """No title label, no sections, fuzzy title also misses -> title=None + warning."""
    text = "我们团队需要一位熟悉大模型的同学加入，待遇从优"
    candidates = extract_jd_candidates(text, "https://x.com")
    assert len(candidates) == 1
    c = candidates[0]
    assert c.title is None
    assert "No job title found via heuristics" in c.normalization_warnings
    assert "No responsibilities or requirements sections found" in c.normalization_warnings
    assert "No location information found" in c.normalization_warnings


def test_extract_jd_candidates_returns_normalized_job_candidate_type() -> None:
    candidates = extract_jd_candidates("岗位名称: 测试岗\n", "https://x.com")
    assert candidates
    assert isinstance(candidates[0], NormalizedJobCandidate)


def test_extract_jd_candidates_feishu_card_list() -> None:
    """A card listing produces one candidate per card from injected headers."""
    text = (
        "座舱Agent Harness算法工程师\n"
        "北京、上海校招正式技术提前批职位 ID：A33756\n"
        "* 探索 agent harness\n"
        "Agent编排平台工程师\n"
        "上海校招正式职位 ID：A33757\n"
        "* 调度与编排\n"
        "多模态研究实习生\n"
        "深圳、杭州实习职位 ID：A33758\n"
        "* 多模态理解\n"
    )
    candidates = extract_jd_candidates(text, "https://nio.jobs.feishu.cn/x")
    assert len(candidates) == 3
    assert [c.title for c in candidates] == [
        "座舱Agent Harness算法工程师",
        "Agent编排平台工程师",
        "多模态研究实习生",
    ]
    # A single 、 does not split (_extract_locations needs 2+ delimiters).
    assert candidates[0].locations == ["北京、上海"]
    assert candidates[0].recruitment_types == ["campus_recruitment"]
    assert candidates[1].locations == ["上海"]
    assert candidates[2].recruitment_types == ["internship"]
    assert all(c.company_name is None for c in candidates)


def test_extract_jd_candidates_caps_at_max_per_page() -> None:
    """A 101-card Feishu listing stops at the 100-candidate page ceiling."""
    cards = [
        f"Agent{i:03d}算法工程师\n"
        f"上海校招正式职位 ID：A{i:05d}\n"
        f"* 职责 {i}\n"
        for i in range(101)
    ]
    candidates = extract_jd_candidates("".join(cards), "https://nio.jobs.feishu.cn/x")
    assert len(candidates) == 100
    assert candidates[0].title == "Agent000算法工程师"
    assert candidates[-1].title == "Agent099算法工程师"


# ---------------------------------------------------------------------------
# branch-coverage: length-guard False arms + fuzzy-title / unstructured arms
# ---------------------------------------------------------------------------


def test_split_multi_job_page_separator_at_start_yields_only_after_segment() -> None:
    # Separator at position 0 -> ``before`` is empty -> only ``after`` returned.
    # Covers the ``if before:`` falsy arm (80->82).
    assert _split_multi_job_page("\n岗位二：后续内容") == ["岗位二：后续内容"]


def test_extract_title_rejects_an_overlong_label_value() -> None:
    # ``岗位名称：`` captures a 90-char value -> len > 80 -> falls through to
    # Pattern 2 (which cannot match this prefix) -> (None, 0.0). Covers 105->101.
    assert _extract_title("岗位名称：" + "A" * 90) == (None, 0.0)


def test_extract_company_rejects_an_overlong_label_value() -> None:
    # ``公司：`` captures a 110-char value -> len > 100 -> falls through.
    # Covers 116->112.
    assert _extract_company("公司：" + "B" * 110) == (None, 0.0)


def test_extract_department_rejects_an_overlong_label_value() -> None:
    # ``部门：`` captures a 90-char value -> len > 80 -> falls through.
    # Covers 127->123.
    assert _extract_department("部门：" + "C" * 90) is None


def test_extract_locations_skips_an_overlong_single_part() -> None:
    # No 2+ delimiter run -> one 60-char part -> len > 50 -> dropped.
    # Covers 176->174.
    assert _extract_locations("工作地点：" + "D" * 60) == []


def test_extract_deadline_rejects_an_overlong_label_value() -> None:
    # ``截止日期：`` captures a 110-char value -> len > 100 -> falls through.
    # Covers 226->222.
    assert _extract_deadline("截止日期：" + "E" * 110) is None


def test_fuzzy_title_pattern2_skips_an_http_candidate() -> None:
    # Pattern 2 matches ``岗位：http://example.com`` but the candidate starts
    # with ``http`` -> guard False -> Pattern 2 skipped, Pattern 5 returns the
    # first line. Covers 428->432.
    assert _fuzzy_extract_title("岗位：http://example.com") == "岗位：http://example.com"


def test_fuzzy_title_pattern3_skips_a_skip_word_line() -> None:
    # ``关注岗位`` ends in 岗位 (Pattern 3 matches) but contains the skip word
    # ``关注`` -> guard False -> falls through. Covers 437->432.
    assert _fuzzy_extract_title("标题\n关注岗位") is None


def test_fuzzy_title_pattern4_skips_an_overlong_capture() -> None:
    # Pattern 4 captures an 82-char raw (50 A's + 招聘 + 30 B's) -> len > 80 ->
    # guard False -> skipped. Pattern 5 then returns the first 60 chars.
    # Covers 448->452.
    result = _fuzzy_extract_title("A" * 50 + "招聘" + "B" * 30)
    assert result is not None
    assert result.startswith("A")


def test_fuzzy_title_pattern5_skips_a_first_line_under_four_chars() -> None:
    # Every earlier pattern misses and the first line ``短`` is 1 char ->
    # ``len >= 4`` False -> Pattern 5 skipped -> None. Covers 453->462.
    assert _fuzzy_extract_title("短") is None


def test_extract_from_unstructured_text_keeps_company_none_without_separator() -> None:
    # Company label absent and the first line has no ``丨`` -> company stays
    # None (the ``丨`` split branch is skipped). Covers 497->502.
    text = (
        "正在招聘AI应用开发工程师岗位，工作地点在北京，欢迎投递简历应聘，"
        "这是一个不错的实习岗位机会，团队氛围好，技术栈先进，发展空间大。"
    )
    candidate = _extract_from_unstructured_text(text, "https://x.com")
    assert candidate is not None
    assert candidate.company_name is None
