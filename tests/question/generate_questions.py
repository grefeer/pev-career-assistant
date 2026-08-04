"""Generate the 100-question PEV evaluation dataset (Q001.json..Q100.json).

Distribution invariants (enforced deterministically, no randomness):

  - 100 questions total.
  - complexity: 50 ``simple`` (single skill) / 50 ``complex`` (multi-skill).
  - site types: 5 categories x 20 questions each (10 simple + 10 complex),
    so no category forms a long tail and the file order itself is balanced.
  - skill mentions (across simple + complex): job-discovery 52, job-matching 50,
    resume-tailoring 42, career-planning 46 -- no long tail.
  - 4 reference profiles rotate evenly (25 questions each).
  - complex questions always carry a time window; job-discovery simple
    questions rotate one of the four windows; matching/tailoring/planning
    simple questions reference already-collected JD evidence.

Each generated file has no reference answer (``reference_answer: null``) --
answers are attached later by the evaluation harness or human reviewer.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

OUT_DIR = pathlib.Path(__file__).resolve().parent

SITE_TYPES = [
    "company-official",
    "state-owned",
    "aggregator",
    "campus",
    "tech-vertical",
]

# name/desc/company/accessibility per site instance. ``gated`` means the
# category commonly shows a login/captcha wall, so a correct run degrades to
# needs_user / needs_manual_review instead of forcing through.
SITE_POOLS: dict[str, list[dict[str, Any]]] = {
    "company-official": [
        {"name": "字节跳动招聘官网", "desc": "字节跳动的官方招聘页（jobs.bytedance.com）", "accessibility": "public"},
        {"name": "小米招聘官网", "desc": "小米集团的官方招聘页", "accessibility": "public"},
        {"name": "美团招聘官网", "desc": "美团的官方招聘页", "accessibility": "public"},
        {"name": "拼多多招聘官网", "desc": "拼多多的官方招聘页", "accessibility": "public"},
        {"name": "小红书招聘官网", "desc": "小红书的官方招聘页", "accessibility": "public"},
        {"name": "网易招聘官网", "desc": "网易的官方招聘页", "accessibility": "public"},
        {"name": "百度招聘官网", "desc": "百度的官方招聘页", "accessibility": "public"},
        {"name": "腾讯招聘官网", "desc": "腾讯的官方招聘页", "accessibility": "public"},
    ],
    "state-owned": [
        {"name": "国聘网", "desc": "国聘网（央国企招聘平台）", "accessibility": "public"},
        {"name": "国家电网招聘平台", "desc": "国家电网招聘平台", "accessibility": "public"},
        {"name": "中国移动招聘官网", "desc": "中国移动招聘官网", "accessibility": "public"},
        {"name": "中国电科招聘平台", "desc": "中国电子科技集团招聘平台", "accessibility": "public"},
        {"name": "中国兵器工业集团招聘", "desc": "中国兵器工业集团招聘平台", "accessibility": "public"},
        {"name": "中国航天人才网", "desc": "中国航天人才网（航天科技/科工招聘）", "accessibility": "public"},
        {"name": "中粮集团招聘", "desc": "中粮集团招聘平台", "accessibility": "public"},
        {"name": "中国建筑招聘平台", "desc": "中国建筑集团招聘平台", "accessibility": "public"},
    ],
    "aggregator": [
        {"name": "Boss 直聘", "desc": "Boss 直聘（综合招聘平台）", "accessibility": "gated"},
        {"name": "拉勾网", "desc": "拉勾网（互联网招聘平台）", "accessibility": "gated"},
        {"name": "智联招聘", "desc": "智联招聘（综合招聘平台）", "accessibility": "gated"},
        {"name": "前程无忧", "desc": "前程无忧（综合招聘平台）", "accessibility": "gated"},
        {"name": "猎聘", "desc": "猎聘（中高端招聘平台）", "accessibility": "gated"},
        {"name": "牛客网", "desc": "牛客网（校招笔试与招聘社区）", "accessibility": "gated"},
    ],
    "campus": [
        {"name": "教育部大学生就业网", "desc": "教育部大学生就业网（ncss.cn）", "accessibility": "public"},
        {"name": "本校就业信息网", "desc": "学校就业指导中心官网/就业信息网", "accessibility": "public"},
        {"name": "校园双选会/宣讲会公告", "desc": "学校就业网发布的宣讲会与双选会信息", "accessibility": "public"},
        {"name": "校招官网（秋招/春招专题页）", "desc": "各企业校招官网与高校秋招专题页", "accessibility": "public"},
        {"name": "高校就业公众号", "desc": "高校就业指导中心公众号推送", "accessibility": "public"},
        {"name": "基层就业项目公告", "desc": "选调生/基层就业项目公告（就业网转载）", "accessibility": "public"},
    ],
    "tech-vertical": [
        {"name": "稀土掘金社区", "desc": "稀土掘金社区的技术招聘帖", "accessibility": "public"},
        {"name": "V2EX 酷工作", "desc": "V2EX 酷工作节点", "accessibility": "public"},
        {"name": "地方政府人才网", "desc": "地方政府人才网/人才引进公告", "accessibility": "public"},
        {"name": "远程工作平台", "desc": "远程工作招聘平台（含出海岗位）", "accessibility": "public"},
        {"name": "技术社区招聘频道", "desc": "技术社区招聘频道/开源项目招聘帖", "accessibility": "public"},
        {"name": "AI 行业垂直招聘渠道", "desc": "AI/大模型行业垂直招聘渠道", "accessibility": "public"},
    ],
}

PROFILES: dict[str, dict[str, Any]] = {
    "P1": {
        "role": "AI 应用开发工程师（应届生）",
        "role_kw": "AI应用开发 大模型 Agent",
        "skills": "Python、LangChain、RAG、Agent",
        "cities": ["北京", "深圳"],
        "exp": "应届生（校招）",
        "job_titles": ["大模型应用开发工程师", "AI 应用开发实习生", "LLM 应用工程师"],
    },
    "P2": {
        "role": "Java 后端开发工程师（3 年经验）",
        "role_kw": "Java后端 微服务",
        "skills": "Java、Spring Cloud、MySQL、Redis",
        "cities": ["上海", "杭州"],
        "exp": "社招（3 年经验）",
        "job_titles": ["Java 后端开发工程师", "后端开发工程师（Java）", "微服务开发工程师"],
    },
    "P3": {
        "role": "前端开发工程师（2 年经验）",
        "role_kw": "前端开发 Vue3",
        "skills": "Vue3、TypeScript、Vite",
        "cities": ["广州", "深圳"],
        "exp": "社招（2 年经验）",
        "job_titles": ["前端开发工程师", "Web 前端工程师", "Vue 前端开发"],
    },
    "P4": {
        "role": "AIGC 产品经理（应届生）",
        "role_kw": "AIGC产品 AI产品经理",
        "skills": "产品设计、AIGC 应用、数据分析",
        "cities": ["北京"],
        "exp": "应届生（校招）",
        "job_titles": ["AIGC 产品经理", "AI 产品经理（实习）", "产品经理（AIGC 方向）"],
    },
}

TIME_WINDOWS = ["recent-1-day", "recent-3-days", "recent-7-days", "recent-30-days"]
TIME_WINDOW_TEXT = {
    "recent-1-day": "最近1天",
    "recent-3-days": "最近3天",
    "recent-7-days": "最近7天",
    "recent-30-days": "最近30天",
}

# --- Templates. One template per question of its combination; placeholders are
# filled from the assigned profile / site / time window. Templates that need a
# job title use {job_title}; those describing discovery use {site_desc}.
SIMPLE_TEMPLATES: dict[str, list[str]] = {
    "job-discovery": [
        "帮我在{site_name}上找{tw}发布的{role}岗位，方向是{role_kw}，地点{city}。",
        "{tw}{site_desc}有哪些{role}岗位？请列出岗位标题和链接。",
        "用关键词“{role_kw}”在{site_name}搜索{role}相关岗位，只要{city}的。",
        "{site_desc}最近有没有适合{exp}的岗位？帮我收集一下{role}方向的。",
        "我想找{city}的{role}岗位，请在{site_name}搜索并汇总岗位信息。",
        "{tw}发布的{role}岗位里，{site_desc}发布的有哪些？我要{city}的。",
        "请到{site_name}看看有没有适合{exp}的岗位，方向{role_kw}，最好在{city}。",
        "{tw}更新过的岗位中，{site_desc}有没有适合我的{role}岗位？",
        "在{site_desc}按“{role_kw}”搜索，把{city}的{role}岗位都列出来。",
        "我要找{role}工作，帮我从{site_name}抓取最近发布的岗位，关键词{role_kw}。",
        "{site_name}上{exp}的{role}岗位有哪些？列出{tw}发布的。",
        "请帮我搜集{site_desc}上{tw}的{role}岗位信息，包括公司、地点和投递链接。",
        "看看{site_name}有没有{role}岗位，{tw}发布的优先。",
    ],
    "job-matching": [
        "最近收集的岗位里，哪些{role}岗位最适合我？按匹配度排序。",
        "从已抓取的 JD 中选出与我匹配度最高的前 3 个岗位。",
        "对比这些候选岗位，我的{skills}经验更适合哪个？",
        "已收集的岗位里，哪个与我的{exp}背景最契合？给出理由。",
        "把最近收集的岗位按我的背景排序，剔除明显不合适的。",
        "这些岗位里，我投{role}方向的成功率如何排序？",
        "基于我的简历，从已抓取岗位中推荐最匹配的 5 个。",
        "已收集岗位中，哪些要求{skills}？帮我挑出匹配的。",
        "帮我评估最近收集的岗位与我的匹配度，输出排名。",
        "从已抓取 JD 中，找出适合{exp}的岗位并排序。",
        "这些岗位里哪些和我期望的{city}与{role}方向一致？",
        "对比已收集岗位与我的背景，列出最值得投递的 3 个。",
    ],
    "resume-tailoring": [
        "针对岗位“{job_title}”帮我定制简历，突出{skills}相关经历。",
        "请根据该 JD 定制我的简历，把{role}项目经验放到最前面。",
        "针对这个岗位，优化我简历中的技能描述，对齐 JD 要求。",
        "我的简历如何调整才能匹配“{job_title}”这个岗位？",
        "根据该 JD 的要求，改写我的项目经历描述。",
        "针对{job_title}岗位，给出简历修改建议（改动点+原因）。",
        "帮我按目标 JD 定制简历：突出{skills}、弱化无关经历。",
        "简历需要针对这个岗位做哪些调整？给出可执行的修改项。",
        "针对该岗位 JD，把简历里的关键词对齐到岗位要求。",
        "为投递“{job_title}”，优化我的简历自我评价与项目部分。",
        "基于该 JD 与我的{exp}背景，定制一份针对性简历。",
        "该岗位要求{skills}，请据此调整我的简历描述。",
    ],
    "career-planning": [
        "针对“{job_title}”岗位，给我一份面试准备计划。",
        "我要面试这个岗位，请制定复习与准备安排。",
        "根据该 JD 的技术栈{skills}，帮我规划面试复习重点。",
        "给出{job_title}岗位的面试建议，包括常见问题与回答要点。",
        "针对该岗位，制定一周内的求职行动计划。",
        "{job_title}面试前我该如何准备？输出详细计划。",
        "根据 JD 内容，准备{role}岗位的自我介绍与项目阐述思路。",
        "给我一份面试追问清单，基于该 JD 的要求。",
        "针对这个岗位，输出面试准备 checklist。",
        "根据 JD 中要求的{skills}，规划我的面试学习路线。",
        "求职{role}方向，请基于目标 JD 制定我的准备计划。",
        "针对该岗位 JD，制定 3 天内的面试冲刺计划。",
        "面试该岗位时如何回答“项目经历”？基于 JD 定制准备方案。",
    ],
}

COMPLEX_TEMPLATES: dict[tuple[str, ...], list[str]] = {
    ("job-discovery", "job-matching", "resume-tailoring", "career-planning"): [
        "{tw}{site_desc}上适合我的{role}岗位有哪些？选出最适合的 2 个，针对每个定制简历，并给出面试建议。",
        "帮我完成一次完整求职：在{site_name}找{tw}的{role}岗位→筛选适合我的→为最匹配岗位定制简历→准备面试。",
        "{tw}在{site_desc}有哪些{role}岗位适合我？给出匹配排名、最优岗位的简历调整方案和面试准备计划。",
        "请执行：搜索{role_kw}岗位（{site_name}，{tw}）→匹配我的背景→针对最佳岗位定制简历→输出面试要点。",
        "{tw}从{site_desc}收集的岗位值得投吗？如果适合，帮我定制简历并准备面试。",
        "找{city}的{role}岗位（{site_name}），按匹配度排序，为前 3 名定制简历并给面试建议。",
        "完整流程：{site_desc}最近岗位收集→按我{exp}背景匹配→为最合适岗位定制简历→制定面试计划。",
        "帮我看看{tw}{site_desc}的{role}岗位，哪个最合适？为它定制简历并给出面试准备方案。",
        "求职任务：在{site_name}搜索→匹配→简历定制→面试准备，针对{role_kw}方向，{tw}。",
        "从{site_desc}{tw}发布的岗位中选出最适合我的，定制简历，并准备对应的面试问题。",
    ],
    ("job-discovery", "job-matching", "career-planning"): [
        "{tw}{site_desc}的{role}岗位中，哪些适合我？给出匹配排名和面试准备建议。",
        "在{site_name}找{tw}的{role}岗位并筛选适合我的，然后为最匹配的岗位制定面试计划。",
        "收集{site_desc}{tw}岗位→按匹配度筛选→为胜出岗位给面试建议。",
        "适合我的{role}岗位有哪些（{site_name}，{tw}）？排序并给出每个岗位的面试要点。",
        "找岗位→匹配→面试规划：在{site_name}按“{role_kw}”搜索{city}的岗位，{tw}。",
        "从{site_desc}最近岗位里选出最适合我的 3 个，并规划各自的面试准备。",
    ],
    ("job-discovery", "job-matching", "resume-tailoring"): [
        "{tw}{site_desc}适合我的{role}岗位有哪些？为最匹配的岗位定制简历。",
        "在{site_name}搜索{role}岗位（{tw}），匹配我的背景后为最佳岗位输出简历修改方案。",
        "收集岗位→筛选→简历定制：在{site_name}找{city}的{role}岗位，{tw}。",
        "从最近收集的岗位（{site_desc}）选出适合我的，针对前 2 个定制简历。",
        "帮我从{site_name}找{tw}的{role}岗位并选出最匹配的，然后定制简历。",
        "{site_name}的{role}岗位里匹配我背景的，请定制对应简历。",
    ],
    ("job-matching", "resume-tailoring", "career-planning"): [
        "从最近收集的岗位中选出最适合我的，定制简历并准备面试。",
        "基于已收集岗位：匹配排序→为最佳岗位定制简历→给出面试建议。",
        "已收集的岗位哪个最适合我？定制简历并制定面试计划。",
        "从候选岗位中选出前 2 匹配，各自定制简历并准备面试要点。",
    ],
    ("job-discovery", "resume-tailoring", "career-planning"): [
        "从{site_desc}找{tw}的{role}岗位，为其中一个定制简历并准备面试。",
        "在{site_name}找一个适合我的{role}岗位（{tw}），定制简历+面试准备。",
        "帮我找{tw}的{role}岗位（{site_desc}），并为最佳选择做简历定制与面试计划。",
        "搜索{role_kw}岗位（{site_name}，{tw}），然后针对它定制简历和准备面试。",
    ],
    ("job-discovery", "job-matching"): [
        "{tw}{site_desc}有哪些{role}岗位适合我？按匹配度排序。",
        "在{site_name}找{tw}的{role}岗位并筛选出最适合我的。",
        "收集{site_desc}{tw}岗位，按我的背景匹配排序。",
        "{city}的{role}岗位（{site_name}，{tw}）哪些适合我？",
        "从{site_desc}{tw}岗位中选出与我{exp}背景最匹配的。",
    ],
    ("job-discovery", "career-planning"): [
        "在{site_name}找{tw}的{role}岗位，为最合适的制定求职计划。",
        "收集{site_desc}{tw}的{role}岗位，并规划面试准备。",
        "在{site_name}找{city}的{role}岗位（{tw}），然后制定行动计划。",
        "搜索{role_kw}岗位（{site_name}），针对目标岗位给出面试建议。",
        "{tw}{site_desc}的{role}岗位如何准备？先找岗位再制定面试计划。",
    ],
    ("job-matching", "career-planning"): [
        "从已收集岗位中选出最适合我的，并给面试准备建议。",
        "候选岗位中哪些值得投？按匹配排序并给出准备计划。",
        "基于已收集岗位，推荐最匹配的岗位并规划面试复习。",
        "比较候选岗位与我的匹配度，为最优选择制定求职准备计划。",
    ],
    ("job-discovery", "resume-tailoring"): [
        "在{site_name}找{tw}的{role}岗位，并为匹配度最高的定制简历。",
        "收集{site_desc}{tw}岗位，针对其中一个定制简历。",
        "在{site_name}找{city}的{role}岗位并定制针对性简历。",
    ],
    ("job-matching", "resume-tailoring"): [
        "从已收集岗位中选出最适合我的，并定制简历。",
        "基于候选岗位的匹配排名，为前 3 名定制简历。",
        "已收集岗位哪个最匹配？输出对应简历定制方案。",
    ],
}

SIMPLE_SKILL_COUNTS = [
    ("job-discovery", 13),
    ("job-matching", 12),
    ("resume-tailoring", 12),
    ("career-planning", 13),
]

COMPLEX_SKILL_SETS = [
    (("job-discovery", "job-matching", "resume-tailoring", "career-planning"), 10),
    (("job-discovery", "job-matching", "career-planning"), 6),
    (("job-discovery", "job-matching", "resume-tailoring"), 6),
    (("job-matching", "resume-tailoring", "career-planning"), 4),
    (("job-discovery", "resume-tailoring", "career-planning"), 4),
    (("job-discovery", "job-matching"), 5),
    (("job-discovery", "career-planning"), 5),
    (("job-matching", "career-planning"), 4),
    (("job-discovery", "resume-tailoring"), 3),
    (("job-matching", "resume-tailoring"), 3),
]


def build_recipes() -> list[dict[str, Any]]:
    """Return the 100 recipes in file order (site type round-robin)."""
    simple: list[dict[str, Any]] = []
    for skill, count in SIMPLE_SKILL_COUNTS:
        for i in range(count):
            simple.append({"complexity": "simple", "skills": [skill], "slot": i})
    complex_: list[dict[str, Any]] = []
    for skills, count in COMPLEX_SKILL_SETS:
        for i in range(count):
            complex_.append({"complexity": "complex", "skills": list(skills), "slot": i})

    by_site: dict[str, dict[str, list[dict[str, Any]]]] = {
        st: {"simple": [], "complex": []} for st in SITE_TYPES
    }
    for idx, recipe in enumerate(simple):
        by_site[SITE_TYPES[idx % len(SITE_TYPES)]]["simple"].append(recipe)
    for idx, recipe in enumerate(complex_):
        by_site[SITE_TYPES[idx % len(SITE_TYPES)]]["complex"].append(recipe)

    recipes: list[dict[str, Any]] = []
    for st in SITE_TYPES:
        s_list = by_site[st]["simple"]
        c_list = by_site[st]["complex"]
        for k in range(10):
            recipes.append(s_list[k])
            recipes.append(c_list[k])
    return recipes


def render_question(recipe: dict[str, Any], site: dict[str, Any], profile: dict[str, Any]) -> str:
    """Pick the combination's template by slot and fill placeholders."""
    if recipe["complexity"] == "simple":
        templates = SIMPLE_TEMPLATES[recipe["skills"][0]]
    else:
        templates = COMPLEX_TEMPLATES[tuple(recipe["skills"])]
    template = templates[recipe["slot"] % len(templates)]

    ctx = {
        "site_name": site["name"],
        "site_desc": site["desc"],
        "role": profile["role"],
        "role_kw": profile["role_kw"],
        "skills": profile["skills"],
        "city": profile["cities"][recipe["slot"] % len(profile["cities"])],
        "exp": profile["exp"],
        "job_title": profile["job_titles"][recipe["slot"] % len(profile["job_titles"])],
        "tw": (
            TIME_WINDOW_TEXT[recipe["time_window"]]
            if recipe["time_window"]
            else ""
        ),
    }
    return template.format(**ctx)


def time_window_for(recipe: dict[str, Any], slot: int) -> tuple[str | None, str | None]:
    """Assign a time window; JM/RT/CP simple questions act on collected JDs."""
    skill = recipe["skills"][0] if recipe["complexity"] == "simple" else None
    if recipe["complexity"] == "complex":
        window = TIME_WINDOWS[slot % len(TIME_WINDOWS)]
        return window, TIME_WINDOW_TEXT[window]
    if skill == "job-discovery":
        window = TIME_WINDOWS[slot % len(TIME_WINDOWS)]
        return window, TIME_WINDOW_TEXT[window]
    return None, None


def main() -> None:
    recipes = build_recipes()
    assert len(recipes) == 100, f"expected 100 recipes, got {len(recipes)}"

    written: list[pathlib.Path] = []
    site_index = {"simple": 0, "complex": 0}
    combo_index = {"simple": 0, "complex": 0}
    for number, recipe in enumerate(recipes, start=1):
        # site type comes from the recipe layout: each (simple, complex) pair
        # in file order belongs to one site category, 20 questions per type.
        site_type = SITE_TYPES[((number - 1) // 2) % len(SITE_TYPES)]
        pool = SITE_POOLS[site_type]
        site = pool[site_index[recipe["complexity"]] % len(pool)]
        site_index[recipe["complexity"]] += 1

        profile = PROFILES[f"P{(number - 1) % 4 + 1}"]
        time_window, time_window_text = time_window_for(recipe, combo_index[recipe["complexity"]])
        combo_index[recipe["complexity"]] += 1
        recipe["time_window"] = time_window

        document = {
            "id": f"Q{number:03d}",
            "question": render_question(recipe, site, profile),
            "meta": {
                "complexity": recipe["complexity"],
                "skills": recipe["skills"],
                "site_types": [site_type],
                "accessibility": site["accessibility"],
                "time_window": time_window,
                "time_window_text": time_window_text,
            },
            "profile": {
                "id": f"P{(number - 1) % 4 + 1}",
                "role": profile["role"],
                "summary": (
                    f"{profile['exp']}，方向：{profile['role_kw']}，"
                    f"技能：{profile['skills']}，城市：{'、'.join(profile['cities'])}"
                ),
            },
            "reference_answer": None,
        }

        path = OUT_DIR / f"{document['id']}.json"
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(path)

    _print_statistics()


def _print_statistics() -> None:
    """Print the distribution invariants so the dataset is self-verifying."""
    import collections

    counts: dict[str, Any] = {
        "total": 0,
        "complexity": collections.Counter(),
        "site_types": collections.Counter(),
        "skills": collections.Counter(),
        "profiles": collections.Counter(),
        "accessibility": collections.Counter(),
        "time_window": collections.Counter(),
    }
    for path in sorted(OUT_DIR.glob("Q*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        counts["total"] += 1
        counts["complexity"][doc["meta"]["complexity"]] += 1
        for st in doc["meta"]["site_types"]:
            counts["site_types"][st] += 1
        for sk in doc["meta"]["skills"]:
            counts["skills"][sk] += 1
        counts["profiles"][doc["profile"]["id"]] += 1
        counts["accessibility"][doc["meta"]["accessibility"]] += 1
        counts["time_window"][doc["meta"]["time_window"]] += 1

    print(f"total questions: {counts['total']}")
    print(f"complexity:      {dict(sorted(counts['complexity'].items()))}")
    print(f"site_types:      {dict(sorted(counts['site_types'].items()))}")
    print(f"skill mentions:  {dict(sorted(counts['skills'].items()))}")
    print(f"profiles:        {dict(sorted(counts['profiles'].items()))}")
    print(f"accessibility:   {dict(sorted(counts['accessibility'].items()))}")
    print(f"time_window:     {dict(sorted(counts['time_window'].items(), key=lambda kv: str(kv[0])))}")


if __name__ == "__main__":
    main()
