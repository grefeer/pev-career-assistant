"""Build the redesigned evaluation question set (2026-08-05).

Redesign decisions (user-approved):
  - A context-dependent questions -> chained questions (collect -> match /
    tailor / plan, max 3 links; link N+1 runs only after link N succeeded).
  - P1 profile replaced by the real resume (R1: 高硕谦, 2027届, AI 算法/应用).
  - B seed-disconnect questions (state-owned third-party info pages) deleted:
    Q003 Q004 Q083 Q118. Q013 kept with cleaned seeds (liepin only).
  - C unrealistic combos (AIGC PM x state-owned) rewritten by company type:
    big-tech AIGC/AI PM campus roles, state-owned realistic roles via iguopin.
  - D time-window questions rephrased around the smartsheet (27届校招内推台账):
    first list companies updated within N days in the sheet, then verify roles
    company by company. Non-sheet sources (juejin/liepin/v2ex) keep their own
    post timestamps as the recency basis.
  - Login-walled platforms (Boss/lagou/zhaopin/51job/ncss) replaced with
    fetchable sources (liepin role landings / v2ex / iguopin / campus).

Output: R###.json (independent questions) + C###.json (chains) in this dir.
Keep items are copied from redesign_analysis/catalog_*.json with per-id
overrides; rewritten items are authored here.

No cheating: seeds come only from the probe-verified seed bank
(eval_runner.SEED_URLS); question texts never embed answers.
"""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
ANALYSIS = ROOT.parent / "redesign_analysis"

# ----------------------------------------------------------------- profiles

RESUME_TEXT = (ROOT / "resume_r1.txt").read_text(encoding="utf-8")

PROFILES: dict[str, dict] = {
    "R1": {
        "id": "R1",
        "role": "AI 算法工程师（应届生）",
        "summary": (
            "应届生（校招），方向：大模型应用 AI Agent 多模态算法，"
            "技能：Python、PyTorch、LangChain、RAG、大模型微调，城市：北京、深圳"
        ),
        "resume_text": RESUME_TEXT,
    },
}

# P2/P3/P4 copied verbatim from the old question docs (stable profiles).
KEEP_PROFILES_FROM = {
    "P2": "tests/question/Q001.json",  # any old doc carrying the profile
}


def load_catalogs() -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for name in ("catalog_1.json", "catalog_2.json"):
        data = json.loads((ANALYSIS / name).read_text(encoding="utf-8"))
        for entry in data:
            merged[entry["id"]] = entry
    return merged


# ----------------------------------------------------------------- keep list
# (id, overrides) -- overrides patch question/meta of the catalog entry.
# Micro-adjustments grounded in eval findings; nothing changes the verdict.

KEEP: dict[str, dict] = {
    "Q011": {},
    "Q013": {
        "question": "针对LLM 应用工程师岗位，给出简历修改建议（改动点+原因）。",
    },
    "Q017": {},
    "Q028": {},
    "Q034": {},
    "Q040": {
        "question": (
            "在猎聘网产品经理专区找北京的 AIGC 产品经理（应届生）岗位，"
            "并定制针对性简历。"
        ),
    },
    "Q045": {},
    "Q046": {},
    "Q055": {},
    "Q057": {},
    "Q071": {},
    "Q081": {
        "question": (
            "我想找北京的 AI 工程师/大模型应用开发方向（应届生）岗位，"
            "请在字节跳动招聘官网搜索并汇总岗位信息。"
        ),
        "meta": {"time_window": None, "time_window_text": None},
    },
    "Q103": {
        "question": (
            "针对腾讯的“Web 前端工程师”岗位，给我一份面试准备计划。"
        ),
    },
    "Q113": {
        "question": (
            "给出 AI 应用开发实习生岗位的面试建议，包括常见问题与回答要点。"
            "请先找到一份该岗位的公开 JD 作为依据。"
        ),
    },
    "Q114": {
        "question": (
            "Java 后端开发工程师面试前我该如何准备？输出详细计划"
            "（先找到一份该岗位的公开 JD 作为依据）。"
        ),
    },
    "Q115": {
        "question": (
            "前端开发工程师面试前我该如何准备？输出详细计划"
            "（先找到一份该岗位的公开 JD 作为依据）。"
        ),
    },
    "Q133": {},
    "Q134": {
        "question": (
            "我的简历如何调整才能匹配“Java 后端开发工程师”这个岗位？"
            "请先找到一份该岗位的公开 JD 再给出定制建议。"
        ),
    },
    "Q143": {
        "question": (
            "稀土掘金社区的技术招聘帖（帖子自带发布时间）中，"
            "有没有适合我的前端开发工程师（2 年经验）岗位？"
        ),
    },
    "Q144": {
        "question": (
            "针对腾讯的产品经理（AIGC 方向）岗位，给出简历修改建议（改动点+原因）。"
        ),
    },
    "Q148": {
        "question": (
            "北京的 AIGC 产品经理（应届生）岗位有哪些适合我？"
            "请在猎聘产品经理专区（含 AIGC 专场）查找并匹配。"
        ),
    },
}

# ----------------------------------------------------------------- rewrites
# (id, profile, question, meta). Meta defaults to public accessibility.

REWRITES: list[dict] = [
    # ---- 台账时间窗题 (R1) ---- 先找近N天更新的公司 -> 逐公司找岗位
    {
        "id": "R001", "profile": "R1",
        "question": (
            "招聘数据源（27届校招内推信息汇总表）中最近1天更新过招聘信息的公司有哪些？"
            "请先列出最近1天更新的公司清单，再为其中适合我的公司核实"
            "AI 算法/AI 应用方向的校招岗位与投递链接。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": "recent-1-day",
                 "time_window_text": "最近1天"},
    },
    {
        "id": "R002", "profile": "R1",
        "question": (
            "在招聘数据源中找出最近3天更新过招聘信息的公司，"
            "从这些公司中查找与我简历匹配的大模型/AI 应用方向校招岗位并汇总"
            "（先找公司，再逐公司核实岗位）。"
        ),
        "meta": {"complexity": "complex", "skills": ["job-discovery", "job-matching"],
                 "site_types": ["company-official"], "time_window": "recent-3-days",
                 "time_window_text": "最近3天"},
    },
    {
        "id": "R003", "profile": "R1",
        "question": (
            "最近7天在招聘数据源中更新过招聘信息的公司里，有没有发布适合我的"
            "AI 应用开发工程师（应届生）岗位？请从数据源出发逐公司核实并验证投递链接。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": "recent-7-days",
                 "time_window_text": "最近7天"},
    },
    {
        "id": "R004", "profile": "R1",
        "question": (
            "从招聘数据源（智能文档）中找出最近30天更新过招聘信息的公司，"
            "其中哪些发布了适合我的大模型应用开发岗位？请验证投递链接可用性。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": "recent-30-days",
                 "time_window_text": "最近30天"},
    },
    {
        "id": "R005", "profile": "R1",
        "question": (
            "我想找深圳的 AI 应用开发工程师（应届生）岗位。招聘数据源中最近3天"
            "更新的公司里有没有相关岗位？请先找出最近3天更新的公司，再逐公司核实。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["campus"], "time_window": "recent-3-days",
                 "time_window_text": "最近3天"},
    },
    {
        "id": "R006", "profile": "R1",
        "question": (
            "招聘数据源中最近1天更新的公司里，小米发布的校招信息适合我吗"
            "（我做过小米 AI 算法实习，方向：大模型/多模态/Agent）？"
            "请核实岗位内容与投递链接。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": "recent-1-day",
                 "time_window_text": "最近1天"},
    },
    # ---- 台账时间窗题 (P4) ----
    {
        "id": "R007", "profile": "P4",
        "question": (
            "招聘数据源中最近1天更新的公司里，有没有适合我的 AIGC 产品经理"
            "（应届生）岗位？先列出最近1天更新的公司清单，再逐公司核实岗位。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": "recent-1-day",
                 "time_window_text": "最近1天"},
    },
    {
        "id": "R008", "profile": "P4",
        "question": (
            "北京的 AIGC 产品经理（应届生）岗位：从招聘数据源中找出最近3天"
            "更新过招聘信息的公司，为其中适合我的公司核实岗位与投递链接。"
        ),
        "meta": {"complexity": "complex", "skills": ["job-discovery", "job-matching"],
                 "site_types": ["company-official"], "time_window": "recent-3-days",
                 "time_window_text": "最近3天"},
    },
    {
        "id": "R009", "profile": "P4",
        "question": (
            "最近7天招聘数据源中更新过招聘信息的公司里，字节跳动、腾讯、百度等"
            "大厂发布了哪些适合我的 AIGC/AI 产品经理（应届生）岗位？请逐公司核实。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": "recent-7-days",
                 "time_window_text": "最近7天"},
    },
    {
        "id": "R010", "profile": "P4",
        "question": (
            "招聘数据源中最近30天更新的公司中，有哪些发布了适合我的"
            "产品经理（AIGC 方向）岗位？请验证投递链接。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": "recent-30-days",
                 "time_window_text": "最近30天"},
    },
    # ---- 大厂 AIGC/AI 产品经理校招 (P4) ----
    {
        "id": "R011", "profile": "P4",
        "question": (
            "字节跳动 2026 秋招有适合我的 AIGC 产品经理（应届生）岗位吗？"
            "请在字节跳动招聘渠道（官网及招聘数据源中的内推信息）搜索并汇总岗位信息。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R012", "profile": "P4",
        "question": (
            "腾讯有 AIGC/AI 产品经理（应届生）岗位吗？请在腾讯招聘官网搜索并核实岗位详情。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R013", "profile": "P4",
        "question": (
            "百度、美团、小米哪个大厂最近有适合我的 AIGC 产品经理（应届生）校招岗位？"
            "请逐一核实岗位并给出投递建议。"
        ),
        "meta": {"complexity": "complex", "skills": ["job-discovery", "job-matching"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R014", "profile": "P4",
        "question": (
            "快手、小红书有没有适合我的 AI 产品经理（应届生）岗位？请核实岗位与投递链接。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
    # ---- 央国企现实岗位 (按公司类型实事求是) ----
    {
        "id": "R015", "profile": "P2",
        "question": (
            "国家电网、中国移动等央国企有没有适合我的 Java 后端开发工程师岗位？"
            "请在国聘网等公开渠道查找并汇总岗位与投递链接。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["state-owned"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R016", "profile": "P2",
        "question": (
            "国聘网上最近发布的 Java 后端开发工程师（3 年经验）岗位有哪些适合我？"
            "请汇总岗位与投递链接。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["state-owned"], "time_window": "recent-7-days",
                 "time_window_text": "最近7天"},
    },
    {
        "id": "R017", "profile": "P3",
        "question": (
            "中国建筑、中粮集团等央国企的招聘渠道里，有没有适合我的"
            "前端开发工程师（2 年经验）岗位？请核实并汇总。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["state-owned"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R018", "profile": "P3",
        "question": (
            "国聘网找广州/深圳的前端开发工程师（2 年经验）岗位，哪个最适合我？"
            "请给出岗位与投递链接。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["state-owned"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R019", "profile": "R1",
        "question": (
            "央国企有没有适合我的 AI 算法工程师（应届生）岗位"
            "（如运营商研究院、央企数科公司）？请核实并如实汇报结果。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["state-owned"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R020", "profile": "P4",
        "question": (
            "央国企的校招里有没有产品经理（应届生）岗位适合我（AIGC 方向是加分项）？"
            "请核实岗位与投递链接。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["state-owned"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R021", "profile": "P2",
        "question": (
            "中国移动、中国联通有没有适合我的 Java 后端开发工程师（社招）岗位？"
            "请通过国聘网/官网等公开渠道核实。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["state-owned"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R022", "profile": "P3",
        "question": (
            "航天科技、中电科系统有没有适合我的前端开发工程师岗位？"
            "请通过国聘网等公开渠道核实。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["state-owned"], "time_window": None,
                 "time_window_text": None},
    },
    # ---- 换源题 (登录墙平台 -> 可抓源) ----
    {
        "id": "R023", "profile": "R1",
        "question": (
            "猎聘网上有没有适合我的大模型应用开发工程师（应届生）岗位？最近7天发布的优先。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["aggregator"], "time_window": "recent-7-days",
                 "time_window_text": "最近7天"},
    },
    {
        "id": "R024", "profile": "P2",
        "question": (
            "猎聘网上找最近7天发布的 Java 后端开发工程师（3 年经验）岗位，"
            "筛选出最适合我的一个。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery", "job-matching"],
                 "site_types": ["aggregator"], "time_window": "recent-7-days",
                 "time_window_text": "最近7天"},
    },
    {
        "id": "R025", "profile": "P3",
        "question": (
            "猎聘网上找广州/深圳的前端开发工程师（2 年经验）岗位，"
            "并为最匹配的岗位定制针对性简历。"
        ),
        "meta": {"complexity": "complex", "skills": ["job-discovery", "resume-tailoring"],
                 "site_types": ["aggregator"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R026", "profile": "P4",
        "question": (
            "猎聘产品经理专区找北京的 AIGC 产品经理（应届生）岗位，哪个最适合我？"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["aggregator"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R027", "profile": "P2",
        "question": (
            "猎聘网后端开发工程师专区找上海的 Java 后端开发工程师"
            "（3 年经验）岗位并汇总。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["aggregator"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R028", "profile": "P3",
        "question": (
            "猎聘网前端开发工程师专区找深圳的前端开发工程师（2 年经验）岗位，"
            "哪个最适合我？"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["aggregator"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R029", "profile": "R1",
        "question": (
            "猎聘网大模型应用开发工程师专区找北京的 AI 算法/AI 应用开发工程师"
            "（应届生）岗位并汇总。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["aggregator"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R030", "profile": "P2",
        "question": (
            "国聘网找杭州的 Java 后端开发工程师（3 年经验）岗位，哪个最适合我？"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["state-owned"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R031", "profile": "P3",
        "question": "国聘网找广州的前端开发工程师（2 年经验）岗位并汇总。",
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["state-owned"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R032", "profile": "R1",
        "question": (
            "稀土掘金社区的技术招聘帖（帖子自带发布时间）里，最近1天有没有适合我的"
            "AI 应用开发工程师（应届生）岗位？"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["tech-vertical"], "time_window": "recent-1-day",
                 "time_window_text": "最近1天"},
    },
    {
        "id": "R033", "profile": "P2",
        "question": (
            "稀土掘金社区最近7天的技术招聘帖中，有没有适合我的 Java 后端开发工程师岗位？"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["tech-vertical"], "time_window": "recent-7-days",
                 "time_window_text": "最近7天"},
    },
    {
        "id": "R034", "profile": "P4",
        "question": (
            "稀土掘金社区最近3天的招聘帖里，有没有适合我的 AIGC 产品经理（应届生）岗位？"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["tech-vertical"], "time_window": "recent-3-days",
                 "time_window_text": "最近3天"},
    },
    # ---- 官网题 (playwright 可渲染) ----
    {
        "id": "R035", "profile": "R1",
        "question": (
            "在腾讯招聘官网搜索大模型/AI 应用方向的校招岗位并汇总岗位信息。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R036", "profile": "R1",
        "question": (
            "在字节跳动招聘官网搜索 AI 工程师/大模型应用开发岗位（应届生）并汇总岗位信息。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R037", "profile": "P2",
        "question": "在腾讯招聘官网搜索 Java 后端开发工程师（社招）岗位并汇总。",
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R038", "profile": "P3",
        "question": "在腾讯招聘官网搜索 Web 前端工程师岗位（校招/社招）并汇总。",
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R039", "profile": "P4",
        "question": (
            "在腾讯招聘官网搜索 AIGC 产品经理/AI 产品经理（应届生）岗位并核实详情。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R040", "profile": "R1",
        "question": "在百度招聘（talent.baidu.com）搜索 AI 算法工程师（应届生）岗位并汇总。",
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
    # ---- 补充独立题 ----
    {
        "id": "R041", "profile": "P4",
        "question": (
            "字节跳动 2026 秋招的 AIGC 产品经理（应届生）岗位，招聘数据源中"
            "最近7天更新过相关信息吗？请核实并验证投递链接。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": "recent-7-days",
                 "time_window_text": "最近7天"},
    },
    {
        "id": "R042", "profile": "R1",
        "question": (
            "招聘数据源中最近1天更新的公司里，腾讯发布的校招信息有适合我的"
            "AI 算法岗位吗？请核实岗位与投递链接。"
        ),
        "meta": {"complexity": "simple", "skills": ["job-discovery"],
                 "site_types": ["company-official"], "time_window": "recent-1-day",
                 "time_window_text": "最近1天"},
    },
    {
        "id": "R043", "profile": "P2",
        "question": (
            "猎聘网找上海 Java 后端开发工程师（3 年经验）岗位，为最匹配的岗位"
            "输出面试准备计划。"
        ),
        "meta": {"complexity": "complex", "skills": ["job-discovery", "career-planning"],
                 "site_types": ["aggregator"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R044", "profile": "P3",
        "question": (
            "针对猎聘网上的前端开发工程师岗位，给我一份面试准备计划"
            "（先找到一份该岗位的公开 JD 作为依据）。"
        ),
        "meta": {"complexity": "simple", "skills": ["career-planning"],
                 "site_types": ["aggregator"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R045", "profile": "P4",
        "question": (
            "针对猎聘网上的产品经理（AIGC 方向）岗位，给出简历修改建议（改动点+原因）。"
        ),
        "meta": {"complexity": "simple", "skills": ["resume-tailoring"],
                 "site_types": ["aggregator"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R046", "profile": "R1",
        "question": (
            "我的简历如何调整才能匹配“大模型应用开发工程师”岗位？"
            "请先找到一份该岗位的公开 JD 再给出定制建议。"
        ),
        "meta": {"complexity": "simple", "skills": ["resume-tailoring"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
    {
        "id": "R047", "profile": "R1",
        "question": (
            "针对“AI 算法工程师”岗位，给我一份面试准备计划"
            "（先找到一份该岗位的公开 JD 作为依据）。"
        ),
        "meta": {"complexity": "simple", "skills": ["career-planning"],
                 "site_types": ["company-official"], "time_window": None,
                 "time_window_text": None},
    },
]

# ----------------------------------------------------------------- chains
# Each link is one PEV run; link N+1 runs only after link N succeeded, with the
# previous link's artifact URLs injected as candidate_urls.

CHAINS: list[dict] = [
    {
        "id": "C001",
        "links": [
            {
                "profile": "R1",
                "question": (
                    "请在猎聘网大模型应用开发工程师专区收集适合我的岗位信息"
                    "（岗位名称、公司、城市、薪资、经验/学历要求），抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "R1",
                "question": (
                    "针对上一环节收集到的岗位，结合我的简历按匹配度排序，"
                    "选出最适合我的一个并说明理由。"
                ),
                "meta": {"complexity": "complex", "skills": ["job-matching"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C002",
        "links": [
            {
                "profile": "R1",
                "question": (
                    "请在百度校园招聘（talent.baidu.com）收集适合我的 AI 算法/"
                    "AI 应用方向校招岗位 JD。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["company-official"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "R1",
                "question": (
                    "基于上一环节收集的岗位和我的简历，为最匹配的岗位生成简历定制化"
                    "修改建议（改动点+原因）。"
                ),
                "meta": {"complexity": "complex", "skills": ["resume-tailoring"],
                         "site_types": ["company-official"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C003",
        "links": [
            {
                "profile": "R1",
                "question": (
                    "请在猎聘网大模型应用开发工程师专区收集适合我的岗位信息"
                    "（岗位名称、公司、城市、薪资、经验/学历要求），抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "R1",
                "question": "结合我的简历，为上一环节收集的岗位按匹配度排序，选出最匹配的一个。",
                "meta": {"complexity": "complex", "skills": ["job-matching"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "R1",
                "question": "针对上一环节选出的最匹配岗位，给我一份面试准备计划。",
                "meta": {"complexity": "complex", "skills": ["career-planning"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C004",
        "links": [
            {
                "profile": "R1",
                "question": (
                    "请在猎聘网大模型应用开发工程师专区收集适合我的 AI 算法/"
                    "AI 应用开发岗位及岗位信息（岗位名称、公司、城市、薪资、要求），"
                    "抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "R1",
                "question": "针对上一环节收集的岗位，结合我的简历按匹配度排序并说明理由。",
                "meta": {"complexity": "complex", "skills": ["job-matching"],
                         "site_types": ["tech-vertical"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C005",
        "links": [
            {
                "profile": "R1",
                "question": (
                    "招聘数据源中最近3天更新的公司里，有没有适合我的 AI 算法/AI 应用"
                    "校招岗位？请先列出最近3天更新的公司，再逐公司核实岗位与投递链接，"
                    "抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["company-official"], "time_window": "recent-3-days",
                         "time_window_text": "最近3天"},
            },
            {
                "profile": "R1",
                "question": (
                    "基于上一环节找到的岗位和我的简历，为最匹配的岗位生成简历定制化"
                    "修改建议（改动点+原因）。"
                ),
                "meta": {"complexity": "complex", "skills": ["resume-tailoring"],
                         "site_types": ["company-official"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C006",
        "links": [
            {
                "profile": "P2",
                "question": (
                    "请在猎聘网 Java 后端开发工程师专区收集适合我的岗位信息"
                    "（岗位名称、公司、城市、薪资、经验/学历要求），抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "P2",
                "question": "针对上一环节收集的岗位，结合我的简历按匹配度排序，选出最匹配的一个。",
                "meta": {"complexity": "complex", "skills": ["job-matching"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C007",
        "links": [
            {
                "profile": "P2",
                "question": (
                    "请在猎聘网 Java 后端开发工程师专区收集适合我的 Java 后端"
                    "开发工程师社招岗位及岗位信息（岗位名称、公司、城市、薪资、"
                    "经验/学历要求），抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "P2",
                "question": "针对上一环节收集的岗位，给我一份面试准备计划。",
                "meta": {"complexity": "complex", "skills": ["career-planning"],
                         "site_types": ["tech-vertical"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C008",
        "links": [
            {
                "profile": "P2",
                "question": (
                    "请在猎聘网 Java 后端开发工程师专区收集适合我的岗位信息"
                    "（岗位名称、公司、城市、薪资、经验/学历要求），抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "P2",
                "question": "结合我的简历，为上一环节收集的岗位按匹配度排序，选出最匹配的一个。",
                "meta": {"complexity": "complex", "skills": ["job-matching"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "P2",
                "question": "基于上一环节选出的最匹配岗位，给出简历定制化修改建议（改动点+原因）。",
                "meta": {"complexity": "complex", "skills": ["resume-tailoring"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C009",
        "links": [
            {
                "profile": "P3",
                "question": (
                    "请在猎聘网前端开发工程师专区收集适合我的岗位信息"
                    "（岗位名称、公司、城市、薪资、经验/学历要求），抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "P3",
                "question": "针对上一环节收集的岗位，结合我的简历按匹配度排序，选出最匹配的一个。",
                "meta": {"complexity": "complex", "skills": ["job-matching"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C010",
        "links": [
            {
                "profile": "P3",
                "question": (
                    "请在猎聘网前端开发工程师专区收集适合我的岗位信息"
                    "（岗位名称、公司、城市、薪资、经验/学历要求），抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "P3",
                "question": "基于上一环节收集的岗位和我的简历，为最匹配的岗位生成简历定制化修改建议。",
                "meta": {"complexity": "complex", "skills": ["resume-tailoring"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C011",
        "links": [
            {
                "profile": "P3",
                "question": (
                    "请在猎聘网前端开发工程师专区收集适合我的前端开发工程师"
                    "（2 年经验）岗位及岗位信息（岗位名称、公司、城市、薪资、"
                    "经验/学历要求），抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "P3",
                "question": "针对上一环节收集的岗位，给我一份面试准备计划。",
                "meta": {"complexity": "complex", "skills": ["career-planning"],
                         "site_types": ["tech-vertical"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C012",
        "links": [
            {
                "profile": "P4",
                "question": (
                    "请在猎聘产品经理专区收集适合我的 AIGC 产品经理（应届生）岗位信息"
                    "（岗位名称、公司、城市、薪资、经验/学历要求），抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "P4",
                "question": "针对上一环节收集的岗位，结合我的简历按匹配度排序，选出最匹配的一个。",
                "meta": {"complexity": "complex", "skills": ["job-matching"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C013",
        "links": [
            {
                "profile": "P4",
                "question": (
                    "请在猎聘产品经理专区收集适合我的 AIGC 产品经理（应届生）岗位信息"
                    "（岗位名称、公司、城市、薪资、经验/学历要求），抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "P4",
                "question": "针对上一环节收集的岗位，给我一份面试准备计划。",
                "meta": {"complexity": "complex", "skills": ["career-planning"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C014",
        "links": [
            {
                "profile": "P4",
                "question": (
                    "请在猎聘产品经理专区收集适合我的 AIGC 产品经理（应届生）岗位信息"
                    "（岗位名称、公司、城市、薪资、经验/学历要求），抓取页面并保存证据。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "P4",
                "question": "结合我的简历，为上一环节收集的岗位按匹配度排序，选出最匹配的一个。",
                "meta": {"complexity": "complex", "skills": ["job-matching"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "P4",
                "question": "基于上一环节选出的最匹配岗位，给出简历定制化修改建议（改动点+原因）。",
                "meta": {"complexity": "complex", "skills": ["resume-tailoring"],
                         "site_types": ["aggregator"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
    {
        "id": "C015",
        "links": [
            {
                "profile": "R1",
                "question": (
                    "请在百度校园招聘（talent.baidu.com）收集适合我的 AI 算法/"
                    "AI 应用方向校招岗位 JD。"
                ),
                "meta": {"complexity": "simple", "skills": ["job-discovery"],
                         "site_types": ["company-official"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "R1",
                "question": "结合我的简历，为上一环节收集的岗位按匹配度排序，选出最匹配的一个。",
                "meta": {"complexity": "complex", "skills": ["job-matching"],
                         "site_types": ["company-official"], "time_window": None,
                         "time_window_text": None},
            },
            {
                "profile": "R1",
                "question": "针对上一环节选出的最匹配岗位，给我一份面试准备计划。",
                "meta": {"complexity": "complex", "skills": ["career-planning"],
                         "site_types": ["company-official"], "time_window": None,
                         "time_window_text": None},
            },
        ],
    },
]

# ----------------------------------------------------------------- emit

META_DEFAULTS = {"complexity": "simple", "accessibility": "public"}


def _old_profile(profile_id: str, catalogs: dict) -> dict:
    """Fetch P1/P2/P3/P4 profile dicts verbatim from the old question docs."""
    for path in sorted(ROOT.parent.glob("Q*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("profile", {}).get("id") == profile_id:
            return doc["profile"]
    raise KeyError(profile_id)


def main() -> None:
    catalogs = load_catalogs()
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []

    # keep items (with per-id overrides)
    for qid, overrides in KEEP.items():
        entry = catalogs[qid]
        profile = _old_profile(entry["profile"], catalogs)
        doc = {
            "id": qid,
            "question": overrides.get("question", entry["question"]),
            "meta": {**entry["meta"], **(overrides.get("meta") or {})},
            "profile": profile,
        }
        out = ROOT / f"{qid}.json"
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest.append({"id": qid, "kind": "keep", "profile": profile["id"]})

    # rewritten independent questions
    for spec in REWRITES:
        profile_id = spec["profile"]
        profile = PROFILES.get(profile_id) or _old_profile(profile_id, catalogs)
        doc = {
            "id": spec["id"],
            "question": spec["question"],
            "meta": {**META_DEFAULTS, **spec["meta"]},
            "profile": profile,
        }
        out = ROOT / f"{spec['id']}.json"
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest.append({"id": spec["id"], "kind": "rewrite", "profile": profile_id})

    # chains
    for spec in CHAINS:
        links = []
        for link in spec["links"]:
            profile_id = link["profile"]
            profile = PROFILES.get(profile_id) or _old_profile(profile_id, catalogs)
            links.append(
                {
                    "question": link["question"],
                    "meta": {**META_DEFAULTS, **link["meta"]},
                    "profile": profile,
                }
            )
        doc = {"id": spec["id"], "chain": links}
        out = ROOT / f"{spec['id']}.json"
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest.append(
            {"id": spec["id"], "kind": "chain", "links": len(links),
             "profile": [link["profile"]["id"] for link in links]}
        )

    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    total_links = sum(m["links"] for m in manifest if m["kind"] == "chain")
    print(f"independent: {sum(1 for m in manifest if m['kind'] != 'chain')}")
    print(f"chains: {sum(1 for m in manifest if m['kind'] == 'chain')} ({total_links} links)")
    print(f"total docs: {len(manifest)} (links counted: {len(manifest) - sum(1 for m in manifest if m['kind'] == 'chain') + total_links})")


if __name__ == "__main__":
    main()
