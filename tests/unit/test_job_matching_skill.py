"""Behavior tests for evidence-bound PEV job matching."""

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_matching import (
    MatchObservedJobsInput,
    match_observed_jobs,
)


def test_match_observed_jobs_ranks_only_context_evidence_by_confirmed_keywords() -> None:
    """Removing evidence or keyword hits must change the user-visible ranking."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "observed_public_evidence": [
                {
                    "artifact_id": "artifact-agent",
                    "source_url": "https://jobs.example/agent",
                    "content_hash": "a" * 64,
                    "title": "AI Agent 开发工程师",
                    "visible_text": "负责 RAG、Agent 平台和 Python 服务开发。",
                },
                {
                    "artifact_id": "artifact-sales",
                    "source_url": "https://jobs.example/sales",
                    "content_hash": "b" * 64,
                    "title": "销售运营专员",
                    "visible_text": "负责销售数据和客户运营。",
                },
            ]
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(profile_keywords=["Python", "RAG", "Agent"]),
    )

    assert [(match.artifact_id, match.score, match.matched_keywords) for match in result.matches] == [
        ("artifact-agent", 100, ["python", "rag", "agent"]),
        ("artifact-sales", 0, []),
    ]
    assert all(match.artifact_id.startswith("artifact-") for match in result.matches)


def test_match_reports_missing_salary_and_company_type_as_unverified_not_inferred() -> None:
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "observed_public_evidence": [{
                "artifact_id": "artifact-agent",
                "source_url": "https://jobs.example/agent",
                "content_hash": "a" * 64,
                "title": "AI Agent 开发工程师",
                "visible_text": "北京岗位，负责 Agent 平台和 Python 服务开发。",
            }]
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(
            profile_keywords=["Python", "Agent"],
            preferred_locations=["北京"],
            ranking_criteria=["skills", "location", "salary", "company_type"],
        ),
    )

    assert result.matches[0].matched_locations == ["北京"]
    assert result.matches[0].compensation_text is None
    assert result.matches[0].unverified_ranking_criteria == ["salary", "company_type"]
    assert result.unresolved_ranking_criteria == ["salary", "company_type"]


def test_match_uses_only_explicit_salary_and_company_type_evidence() -> None:
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "observed_public_evidence": [
                None,
                {"artifact_id": "missing-fields"},
                {
                    "artifact_id": "agent", "source_url": "https://jobs.example/agent",
                    "visible_text": "上海民营企业，月薪 25k-35k，负责 Python Agent。",
                },
            ]
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(
            profile_keywords=[" Python ", "python"], preferred_locations=[" 上海 ", "上海"],
            ranking_criteria=["salary", "company_type", "recency", "salary"], limit=1,
        ),
    )

    assert result.matches[0].compensation_text == "25k-35k"
    assert result.matches[0].observed_company_types == ["民营"]
    assert result.matches[0].matched_locations == ["上海"]
    assert result.matches[0].unverified_ranking_criteria == ["recency"]
    assert result.unresolved_ranking_criteria == ["recency"]


def test_match_rejects_non_list_evidence_without_inventing_results() -> None:
    result = match_observed_jobs(
        ToolContext(user_id="user-a", run_id="run-a", metadata={"observed_public_evidence": "bad"}),
        MatchObservedJobsInput(ranking_criteria=["recency"]),
    )

    assert result.matches == []
    assert result.unresolved_ranking_criteria == []


def test_match_marks_an_unmatched_requested_location_as_unverified() -> None:
    context = ToolContext(
        user_id="user-a", run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "agent", "source_url": "https://jobs.example/agent",
            "visible_text": "上海岗位，负责 Agent 开发。",
        }]},
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(preferred_locations=["北京"], ranking_criteria=["location"]),
    )

    assert result.matches[0].unverified_ranking_criteria == ["location"]


def test_match_prefers_structured_candidates_over_raw_page_evidence() -> None:
    """A card-list page must produce per-job ranked matches, never one aggregated entry."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "observed_public_evidence": [{
                "artifact_id": "page",
                "source_url": "https://nio.example/campus?limit=10",
                "content_hash": "p" * 64,
                "title": "蔚来校招",
                "visible_text": "提前批-座舱Agent Harness算法工程师\n提前批-Agent开发工程师-NOMI",
            }],
            "structured_job_candidates": [
                {
                    "artifact_id": "cand-a",
                    "source_url": "https://nio.example/apply/1",
                    "content_hash": "a" * 64,
                    "title": "提前批-座舱Agent Harness算法工程师",
                    "locations": ["上海"],
                    "responsibilities": "负责 Agent Harness 与 RAG 平台。",
                    "requirements": "",
                },
                {
                    "artifact_id": "cand-b",
                    "source_url": "https://nio.example/apply/2",
                    "content_hash": "b" * 64,
                    "title": "提前批-Agent开发工程师-NOMI",
                    "locations": ["北京"],
                    "responsibilities": "",
                    "requirements": "负责 Agent 应用开发与测试。",
                },
            ],
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(profile_keywords=["Agent"]),
    )

    by_id = {match.artifact_id: match for match in result.matches}
    assert set(by_id) == {"cand-a", "cand-b"}
    assert by_id["cand-a"].title == "提前批-座舱Agent Harness算法工程师"
    assert by_id["cand-b"].title == "提前批-Agent开发工程师-NOMI"
    assert all(match.score == 34 for match in result.matches)
    assert by_id["cand-a"].source_url == "https://nio.example/apply/1"
    assert by_id["cand-a"].evidence_excerpt == "负责 Agent Harness 与 RAG 平台。"
    # The raw page must not be ranked as a job on top of its own candidates.
    assert all(match.artifact_id != "page" for match in result.matches)


def test_match_structured_candidates_score_from_sections_and_locations() -> None:
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "structured_job_candidates": [{
                "artifact_id": "cand",
                "source_url": "https://jobs.example/apply",
                "content_hash": "c" * 64,
                "title": "AI Agent 开发工程师",
                "locations": ["北京、上海"],
                # Card-list extraction may land JD snippets here; they count as
                # evidence but never as a trusted company fact.
                "company_name": "自研 Agent harness 框架的全链路设计",
                "responsibilities": "负责 RAG、Agent 平台和 Python 服务开发，月薪 25k-35k，上海民营公司。",
                "requirements": "",
            }]
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(
            profile_keywords=["Python", "RAG", "Agent"],
            preferred_locations=["上海"],
            ranking_criteria=["skills", "location", "salary", "company_type", "recency"],
        ),
    )

    match = result.matches[0]
    assert match.title == "AI Agent 开发工程师"
    assert match.score == 100
    assert match.matched_keywords == ["python", "rag", "agent"]
    assert match.matched_locations == ["上海"]
    assert match.compensation_text == "25k-35k"
    assert match.observed_company_types == ["民营"]
    assert match.unverified_ranking_criteria == ["recency"]
    assert result.unresolved_ranking_criteria == ["recency"]


def test_match_structured_candidates_skips_malformed_entries() -> None:
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "structured_job_candidates": [
                None,
                {"artifact_id": "no-source"},
                {"source_url": "https://jobs.example/apply", "title": "no-id"},
                {
                    "artifact_id": "ok",
                    "source_url": "https://jobs.example/ok",
                    "content_hash": "o" * 64,
                    "title": "Agent 开发工程师",
                    "locations": "上海",  # non-list locations must not crash
                    "company_name": 789,  # non-str company_name must not crash
                    "responsibilities": 123,  # non-str sections must not crash
                    "requirements": 456,
                },
            ]
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(profile_keywords=["Agent"]),
    )

    assert len(result.matches) == 1
    assert result.matches[0].artifact_id == "ok"
    assert result.matches[0].title == "Agent 开发工程师"
    # No str sections available, so the excerpt falls back to the title.
    assert result.matches[0].evidence_excerpt == "Agent 开发工程师"


def test_match_structured_candidates_falls_back_to_raw_evidence_when_empty() -> None:
    for candidates in ([], "not-a-list"):
        context = ToolContext(
            user_id="user-a",
            run_id="run-a",
            metadata={
                "observed_public_evidence": [{
                    "artifact_id": "page",
                    "source_url": "https://jobs.example/agent",
                    "content_hash": "p" * 64,
                    "title": "AI Agent 开发工程师",
                    "visible_text": "负责 Agent 平台和 Python 服务开发。",
                }],
                "structured_job_candidates": candidates,
            },
        )

        result = match_observed_jobs(
            context,
            MatchObservedJobsInput(profile_keywords=["Agent"]),
        )

        assert [m.artifact_id for m in result.matches] == ["page"]


def test_match_structured_candidates_respects_limit() -> None:
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "structured_job_candidates": [
                {
                    "artifact_id": f"cand-{index}",
                    "source_url": f"https://jobs.example/{index}",
                    "content_hash": str(index) * 64,
                    "title": f"Agent 岗位 {index}",
                    "locations": [],
                    "responsibilities": "",
                    "requirements": "",
                }
                for index in range(3)
            ]
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(profile_keywords=["Agent"], limit=2),
    )

    assert [m.artifact_id for m in result.matches] == ["cand-0", "cand-1", "cand-2"]
    assert len(result.matches) == 3


def test_match_accepts_full_extraction_limit_of_one_hundred() -> None:
    """The ranking limit must cover a full card-list extraction (100 per page)."""
    assert MatchObservedJobsInput(limit=100).limit == 100
    assert MatchObservedJobsInput(limit=101).limit == 100


def test_match_respects_explicit_official_channel_constraints() -> None:
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "task_goal": "中国移动、中国联通有没有适合我的 Java 后端开发工程师岗位？请通过国聘网/官网核实。",
            "structured_job_candidates": [
                {
                    "artifact_id": "liepin",
                    "source_url": "https://www.liepin.com/job/1.shtml",
                    "source_quality": "jd_complete",
                    "title": "Java 后端开发工程师",
                    "locations": ["北京"],
                    "responsibilities": "Java 服务开发",
                    "requirements": "",
                },
                {
                    "artifact_id": "official",
                    "source_url": "https://job.10086.cn/personal/job/detail.html?id=1",
                    "source_quality": "jd_complete",
                    "title": "Java 后端开发工程师",
                    "locations": ["北京"],
                    "responsibilities": "Java 服务开发",
                    "requirements": "",
                },
            ],
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(profile_keywords=["Java", "后端"], preferred_locations=["北京"]),
    )

    assert [match.artifact_id for match in result.matches] == ["official"]


def test_match_ignores_recommendation_cards_from_detail_page() -> None:
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "confirmed_profile_facts": {
                "basics.name": "AIGC 产品经理（应届生）",
                "skills": ["AIGC 应用"],
            },
            "structured_job_candidates": [
                {
                    "artifact_id": "main",
                    "candidate_id": "artifact:candidate:0",
                    "source_quality": "jd_complete",
                    "source_url": "https://jobs.example/detail/1",
                    "page_source_url": "https://jobs.example/detail/1",
                    "page_title": "数字化产品经理招聘",
                    "title": "数字化产品经理",
                    "locations": ["上海"],
                    "responsibilities": "AIGC 应用产品设计",
                    "requirements": "",
                },
                {
                    "artifact_id": "recommended",
                    "candidate_id": "artifact:candidate:1",
                    "source_quality": "jd_complete",
                    "source_url": "https://jobs.example/detail/2",
                    "page_source_url": "https://jobs.example/detail/1",
                    "page_title": "数字化产品经理招聘",
                    "title": "Salesforce产品经理 – Customer Data",
                    "locations": ["上海"],
                    "responsibilities": "AIGC 应用产品设计",
                    "requirements": "",
                },
            ],
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(profile_keywords=["产品经理", "AIGC"]),
    )

    assert [match.artifact_id for match in result.matches] == ["main"]
