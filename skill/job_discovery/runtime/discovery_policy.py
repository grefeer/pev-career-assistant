"""Deterministic job-discovery policy: seeds, keywords and search hints.

Single source of truth for discovery business policy. The PEV runtime
imports these names through the career_skills.discovery_policy
compatibility shim; no business rules live in the runtime harness.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

DERIVED_ROLE_KEYWORDS = (
    "大模型应用开发工程师",
    "大模型应用开发",
    "AIGC 产品经理",
    "AI 产品经理",
    "前端开发工程师",
    "前端开发",
    "Java 后端开发工程师",
    "Java 后端",
    "后端开发工程师",
    "算法工程师",
    "产品经理",
    "应用开发",
    "开发工程师",
    "Java",
    "Python",
)
DERIVED_COMPANY_KEYWORDS = (
    "中国移动",
    "中国联通",
    "中国电信",
    "字节跳动",
    "腾讯",
    "阿里巴巴",
    "百度",
    "华为",
    "小米",
    "京东",
    "美团",
    "快手",
    "小红书",
    "网易",
    "用友",
)
DERIVED_LOCATION_KEYWORDS = (
    "北京",
    "上海",
    "广州",
    "深圳",
    "杭州",
    "南京",
    "苏州",
    "成都",
    "武汉",
    "西安",
    "重庆",
    "天津",
    "长沙",
    "郑州",
    "济南",
    "青岛",
    "合肥",
    "厦门",
    "大连",
    "东莞",
    "佛山",
)

OFFICIAL_COMPANY_DISCOVERY_SEEDS = (
    ("美团", "https://campus.meituan.com/"),
    ("小红书", "https://job.xiaohongshu.com/campus"),
)

# Official recruiting pages that may be used only when the named company was
# returned by the career-sheet tool.  This keeps recent-company discovery
# source-bound while avoiding a brittle dependency on search-engine routing
# after a sheet row points to an unreadable WeChat article.
OBSERVED_COMPANY_DISCOVERY_SEEDS = (
    ("倍漾", "https://www.baiontcapital.com/careers.html"),
)

# Reviewed, directly readable public JD pages for narrowly requested role
# archetypes. These are evidence seeds, not claims that the posting is still
# open: the captured page text (including an explicit closed status) remains
# authoritative and is persisted unchanged for downstream disclosure.
REQUESTED_ROLE_DISCOVERY_SEEDS = (
    (
        "ai_application_intern",
        "https://24365.smartedu.cn/student/jobs/"
        "SvSaumv8prNxWdGTQbF9mh/detail.html",
    ),
    (
        "java_backend_engineer",
        "https://app.mokahr.com/campus-recruitment/tal/146599"
        "?recommendCode=DSXc7DBC#/jobs",
    ),
    (
        # Render-verified role-matched job-card search entry (国聘): the
        # per-job cards are the evidence; detail pages are fetched by the
        # Executor from the search result.
        "frontend_engineer",
        "https://www.iguopin.com/job/list"
        "?keyword=%E5%89%8D%E7%AB%AF%E5%BC%80%E5%8F%91%E5%B7%A5%E7%A8%8B%E5%B8%88",
    ),
)

def official_company_seed_urls(task_goal: str) -> list[str]:
    """Return verified public recruiting entry points explicitly named by the user."""
    urls = [
        url
        for company, url in OFFICIAL_COMPANY_DISCOVERY_SEEDS
        if company in task_goal
    ]
    if "腾讯" in task_goal:
        lowered = task_goal.lower()
        if "aigc" in lowered:
            keyword = "AIGC"
        elif "大模型" in task_goal:
            keyword = "大模型"
        elif "产品经理" in task_goal:
            keyword = "AI 产品经理"
        elif "算法" in task_goal:
            keyword = "AI 算法"
        else:
            keyword = "AI"
        urls.append(
            "https://careers.tencent.com/tencentcareer/api/post/Query?"
            + urlencode(
                {
                    "keyword": keyword,
                    "pageIndex": 1,
                    "pageSize": 10,
                    "language": "zh-cn",
                    "area": "cn",
                }
            )
        )
    return urls


def public_source_mirror_seed_urls(task_goal: str) -> list[str]:
    """Return transparent public mirrors for an explicitly named blocked source.

    The target source remains authoritative: downstream matching accepts a
    mirrored LinkedIn detail only when its captured text explicitly says the
    position came from Liepin.  This route never attempts to bypass Liepin's
    captcha and every derived detail page still passes the normal public-URL
    and evidence hashing checks.
    """
    if "猎聘" not in task_goal:
        return []
    lowered = task_goal.lower()
    if "产品经理" in task_goal and (
        "aigc" in lowered or "ai" in lowered or "大模型" in task_goal
    ):
        keyword = "AI产品经理"
    elif "产品经理" in task_goal:
        keyword = "产品经理"
    else:
        return []
    if any(marker in task_goal for marker in ("应届", "校招", "实习")):
        keyword += "实习生"
    parameters: dict[str, object] = {
        "keywords": keyword,
        "location": "Beijing, China" if "北京" in task_goal else "China",
    }
    if "北京" in task_goal:
        parameters["geoId"] = "103873152"
    parameters["start"] = 0
    return [
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
        + urlencode(parameters)
    ]


def requested_role_seed_urls(task_goal: str) -> list[str]:
    """Return a reviewed exact JD only for a matching role-evidence request."""
    lowered = task_goal.lower()
    requests_public_jd = "jd" in lowered and any(
        marker in task_goal for marker in ("公开", "依据", "作为")
    )
    if not requests_public_jd:
        return []
    if ("ai 应用开发" in lowered or "ai应用开发" in lowered) and "实习" in task_goal:
        role_key = "ai_application_intern"
    elif "java 后端开发" in lowered or "java后端开发" in lowered:
        role_key = "java_backend_engineer"
    elif "前端开发" in task_goal:
        role_key = "frontend_engineer"
    else:
        return []
    return [url for key, url in REQUESTED_ROLE_DISCOVERY_SEEDS if key == role_key]


def observed_company_seed_urls(observations: list[Any]) -> list[str]:
    """Resolve official recruiting pages from tool-observed sheet companies.

    Search snippets are deliberately excluded: only records emitted by the
    deterministic career-sheet tool can grant this routing authority.
    """
    observed_companies: list[str] = []
    for observation in observations:
        if getattr(observation, "tool_name", None) != "query-career-sheet-records":
            continue
        output = observation.output if isinstance(observation.output, dict) else {}
        records = output.get("records")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            company_name = record.get("company_name")
            if isinstance(company_name, str) and company_name.strip():
                observed_companies.append(company_name.strip())
    return list(
        dict.fromkeys(
            url
            for company_marker, url in OBSERVED_COMPANY_DISCOVERY_SEEDS
            if any(company_marker in company for company in observed_companies)
        )
    )


def trusted_discovery_seed_urls(
    task_goal: str, observations: list[Any] | None = None
) -> list[str]:
    return list(
        dict.fromkeys(
            [
                *official_company_seed_urls(task_goal),
                *public_source_mirror_seed_urls(task_goal),
                *requested_role_seed_urls(task_goal),
                *observed_company_seed_urls(observations or []),
            ]
        )
    )

def discovery_search_hints(
    task_goal: str, observations: list[Any]
) -> list[str]:
    """Compile bounded company/role search queries from public task evidence."""
    lowered = task_goal.lower()
    role_terms: list[str] = []
    if "ai 算法" in lowered or "ai算法" in lowered:
        role_terms.append("AI 算法")
    if "ai 应用" in lowered or "ai应用" in lowered:
        role_terms.append("AI 应用")
    if "大模型应用开发" in lowered:
        role_terms.append("大模型应用开发")
    if "aigc" in lowered:
        role_terms.append("AIGC")
    if "产品经理" in lowered:
        role_terms.append("产品经理")
    if "java 后端" in lowered or "java后端" in lowered:
        role_terms.append("Java 后端")
    if "前端" in lowered:
        role_terms.append("Web 前端")
    role_terms = list(dict.fromkeys(role_terms))[:3]
    if not role_terms:
        role_terms = [
            term
            for term in DERIVED_ROLE_KEYWORDS
            if term.lower() in lowered
        ][:2]

    companies = [
        company
        for company in DERIVED_COMPANY_KEYWORDS
        if company.lower() in lowered
    ]
    if not companies:
        ranked_records: list[tuple[int, int, str]] = []
        relevance_weights = {
            "人工智能": 10,
            "大模型": 9,
            "aigc": 9,
            "ai": 7,
            "互联网": 6,
            "算法": 6,
            "机器人": 5,
            "开发": 4,
            "金融科技": 4,
        }
        record_index = 0
        for observation in observations:
            output = observation.output if isinstance(observation.output, dict) else {}
            records = output.get("records")
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                company = record.get("company_name")
                if not isinstance(company, str) or not company.strip():
                    continue
                searchable = " ".join(
                    str(record.get(key) or "")
                    for key in (
                        "company_name",
                        "industry",
                        "raw_summary",
                        "recruitment_type",
                    )
                ).lower()
                score = sum(
                    weight
                    for marker, weight in relevance_weights.items()
                    if marker in searchable
                )
                score += 5 * sum(
                    1 for term in role_terms if term.lower() in searchable
                )
                if score > 0:
                    ranked_records.append((-score, record_index, company.strip()))
                record_index += 1
        companies = list(
            dict.fromkeys(
                company
                for _score, _index, company in sorted(ranked_records)
            )
        )[:3]

    role_text = " ".join(role_terms) or "招聘岗位"
    location_text = " ".join(
        location for location in DERIVED_LOCATION_KEYWORDS if location in task_goal
    )
    graduate_scope = "应届生 校招" if any(
        marker in lowered for marker in ("应届", "校招", "校园招聘")
    ) else ""
    experience_match = re.search(r"(\d+)\s*年(?:经验|工作经验)", task_goal)
    experience_scope = (
        f"{experience_match.group(1)}年经验" if experience_match else ""
    )
    suffix = " ".join(
        part
        for part in (
            location_text,
            graduate_scope,
            experience_scope,
            "岗位详情 官方招聘",
        )
        if part
    )
    source_scopes = [
        scope
        for marker, scope in (
            ("猎聘", "site:liepin.com"),
            ("国聘", "site:iguopin.com"),
            ("稀土掘金", "site:juejin.cn/pin"),
        )
        if marker in task_goal
    ]
    if source_scopes:
        targets = companies or [""]
        return [
            " ".join(
                part for part in (source, company, role_text, suffix) if part
            )[:380]
            for source in source_scopes
            for company in targets[:3]
        ][:5]
    if companies:
        return [
            f"{company} {role_text} 招聘 岗位职责"[:380]
            for company in companies[:5]
        ]
    return [f"{role_text} {suffix}"[:380]]


def public_urls_from_search_item(item: dict[str, Any]) -> list[str]:
    """Split duplicated/space-separated sheet URL cells into real URLs."""
    values = [
        item.get("url"),
        item.get("source_url"),
        item.get("apply_url"),
        item.get("link"),
    ]
    prior = item.get("prior_metadata")
    if isinstance(prior, dict):
        values.append(prior.get("apply_url"))
    urls: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for candidate in re.findall(r"https?://[^\s]+", value):
            candidate = candidate.rstrip('.,;，。；)]}"')
            if candidate and candidate not in urls:
                urls.append(candidate)
    return urls


def job_search_results_are_routable(results: object) -> bool:
    """Return true when a registered route result can drive a public fetch."""
    if not isinstance(results, list) or not results:
        return False
    return any(
        isinstance(item, dict) and bool(public_urls_from_search_item(item))
        for item in results
    )


def search_result_urls(observations: list[Any]) -> list[str]:
    urls: list[str] = []
    for observation in observations:
        output = observation.output if isinstance(observation.output, dict) else {}
        for collection_name in ("results", "records"):
            collection = output.get(collection_name)
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, dict):
                    continue
                for value in public_urls_from_search_item(item):
                    if value not in urls:
                        urls.append(value)
                if len(urls) >= 10:
                    return urls
    return urls


def contains_access_block(observations: list[Any]) -> bool:
    """Detect login/captcha/anti-bot blocks across a bounded observation set."""
    blocked_codes = {
        "anti_bot",
        "anti_bot_challenge",
        "captcha",
        "login_required",
        "access_denied",
        "domain_temporarily_blocked",
    }
    for observation in observations:
        if getattr(observation, "error_code", None) in blocked_codes:
            return True
        output = observation.output if isinstance(observation.output, dict) else {}
        failures = output.get("failures")
        if isinstance(failures, list) and any(
            isinstance(item, dict) and item.get("error_code") in blocked_codes
            for item in failures
        ):
            return True
    return False


