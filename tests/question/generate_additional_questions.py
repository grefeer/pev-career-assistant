"""Generate supplementary questions (Q101..Q150) to balance per-site skill mixes.

The original 100 files (Q001..Q100) are left untouched. Their per-site buckets
are skewed (e.g. company-official simple questions are only job-discovery /
resume-tailoring), so this script appends 10 questions per site category
(5 simple + 5 complex) to give every site bucket coverage of all four skills
and of most complex combinations.

Invariants (deterministic, no randomness):

  - 50 new files Q101..Q150 -> total 150, site categories stay uniform
    (5 x 30), complexity balanced (75 simple / 75 complex).
  - simple additions:  JD 8, RT 7, JM 4, CP 6 (fills the missing skills).
  - complex additions: JD 16, JM 18, RT 15, CP 15 (covers previously absent
    combination types, e.g. full-chain in aggregator/campus/tech-vertical).
  - duplicate questions (normalized whitespace) against the existing 100 and
    against earlier additions are re-rendered with a different slot instead of
    being written.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import generate_questions as gq

OUT_DIR = gq.OUT_DIR

FULL_CHAIN = ("job-discovery", "job-matching", "resume-tailoring", "career-planning")

# Per site category: which simple skills to add (5 each).
SIMPLE_ADD: dict[str, list[str]] = {
    "company-official": ["job-matching", "job-matching", "career-planning", "career-planning", "career-planning"],
    "state-owned": ["job-matching", "job-matching", "career-planning", "career-planning", "career-planning"],
    "aggregator": ["job-discovery", "job-discovery", "resume-tailoring", "resume-tailoring", "resume-tailoring"],
    "campus": ["job-discovery", "job-discovery", "job-discovery", "resume-tailoring", "resume-tailoring"],
    "tech-vertical": ["job-discovery", "job-discovery", "job-discovery", "resume-tailoring", "resume-tailoring"],
}

# Per site category: which complex combinations to add (5 each).
COMPLEX_ADD: dict[str, list[tuple[str, ...]]] = {
    "company-official": [
        ("job-discovery", "job-matching", "resume-tailoring"),
        ("job-discovery", "job-matching"),
        ("job-matching", "career-planning"),
        ("job-discovery", "resume-tailoring"),
        ("job-matching", "resume-tailoring"),
    ],
    "state-owned": [
        ("job-discovery", "resume-tailoring", "career-planning"),
        ("job-matching", "resume-tailoring", "career-planning"),
        ("job-discovery", "career-planning"),
        ("job-matching", "career-planning"),
        ("job-matching", "resume-tailoring"),
    ],
    "aggregator": [
        FULL_CHAIN,
        ("job-discovery", "resume-tailoring", "career-planning"),
        ("job-matching", "resume-tailoring", "career-planning"),
        ("job-discovery", "job-matching"),
        ("job-discovery", "resume-tailoring"),
    ],
    "campus": [
        FULL_CHAIN,
        ("job-matching", "resume-tailoring", "career-planning"),
        ("job-discovery", "resume-tailoring", "career-planning"),
        ("job-discovery", "job-matching"),
        ("job-matching", "resume-tailoring"),
    ],
    "tech-vertical": [
        FULL_CHAIN,
        ("job-discovery", "job-matching", "career-planning"),
        ("job-discovery", "job-matching"),
        ("job-discovery", "career-planning"),
        ("job-matching", "career-planning"),
    ],
}


# Extra templates for combinations whose base templates carry no site
# placeholder (job-matching/career-planning, job-matching/resume-tailoring/
# career-planning, job-matching/resume-tailoring): the existing files already
# use every base template, so new files need fresh wording.
EXTRA_TEMPLATES: dict[tuple[str, ...], list[str]] = {
    ("job-matching", "career-planning"): [
        "在已收集的岗位中，帮我挑出最值得投递的 2 个并给出准备建议。",
        "把已收集岗位按我的匹配度排序，输出排名和面试准备要点。",
        "基于已收集岗位，推荐与我背景最契合的岗位并给出求职准备计划。",
    ],
    ("job-matching", "resume-tailoring", "career-planning"): [
        "在已收集岗位中选出最匹配的，输出简历调整方案和面试准备清单。",
        "从已收集岗位里挑出前 2 个最合适的，针对它们定制简历并准备面试。",
        "基于已收集岗位做完整求职准备：匹配排序、简历定制、面试建议。",
    ],
    ("job-matching", "resume-tailoring"): [
        "在已收集岗位中选出最匹配的，针对它定制简历。",
        "从已收集岗位里挑出最适合我的，输出简历定制方案。",
        "基于已收集岗位的匹配排名，为最匹配的岗位定制简历。",
    ],
}

TEMPLATES: dict[tuple[str, ...], list[str]] = {
    combo: list(templates) for combo, templates in gq.COMPLEX_TEMPLATES.items()
}
for combo, extra in EXTRA_TEMPLATES.items():
    TEMPLATES[combo].extend(extra)


def normalize(text: str) -> str:
    """Whitespace-insensitive fingerprint for duplicate detection."""
    return "".join(text.split())


def render(recipe: dict[str, Any], site: dict[str, Any], profile: dict[str, Any]) -> str:
    """Render one question with the same slot-driven logic as generate_questions."""
    if recipe["complexity"] == "simple":
        templates = gq.SIMPLE_TEMPLATES[recipe["skills"][0]]
    else:
        templates = TEMPLATES[tuple(recipe["skills"])]
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
            gq.TIME_WINDOW_TEXT[recipe["time_window"]]
            if recipe["time_window"]
            else ""
        ),
    }
    return template.format(**ctx)


def build_added_recipes() -> list[dict[str, Any]]:
    """Return the 50 new recipes in (site category -> simple, then complex) order."""
    recipes: list[dict[str, Any]] = []
    for st in gq.SITE_TYPES:
        for skill in SIMPLE_ADD[st]:
            recipes.append({"complexity": "simple", "skills": [skill], "site_type": st})
        for skills in COMPLEX_ADD[st]:
            recipes.append({"complexity": "complex", "skills": list(skills), "site_type": st})
    return recipes


def main() -> None:
    existing_docs: list[dict[str, Any]] = []
    for path in sorted(OUT_DIR.glob("Q*.json")):
        existing_docs.append(json.loads(path.read_text(encoding="utf-8")))

    seen: set[str] = set()
    per_combination_count: dict[str, int] = {}
    for doc in existing_docs:
        seen.add(normalize(doc["question"]))
        key = tuple(doc["meta"]["skills"])
        per_combination_count[key] = per_combination_count.get(key, 0) + 1

    start_number = len(existing_docs) + 1  # 101
    site_index = {"simple": 0, "complex": 0}
    for doc in existing_docs:
        site_index[doc["meta"]["complexity"]] += 1

    written: list[pathlib.Path] = []
    for offset, recipe in enumerate(build_added_recipes()):
        number = start_number + offset
        complexity = recipe["complexity"]
        site_type = recipe["site_type"]
        pool = gq.SITE_POOLS[site_type]

        slot_base = per_combination_count.get(tuple(recipe["skills"]), 0)
        profile = gq.PROFILES[f"P{(number - 1) % 4 + 1}"]
        time_window, time_window_text = gq.time_window_for(recipe, slot_base)
        recipe["time_window"] = time_window

        question = None
        # Slot steps by 1 so every template variant is tried (a larger stride
        # like len(pool) only revisits a few templates, which can all collide
        # with existing P1/P3-only simple files).
        for attempt in range(200):
            slot = slot_base + attempt
            recipe["slot"] = slot
            site = pool[site_index[complexity] % len(pool)]
            question = render(recipe, site, profile)
            if normalize(question) not in seen:
                break
        else:
            raise RuntimeError(f"cannot de-duplicate question for {site_type} {recipe['skills']}")
        seen.add(normalize(question))
        site_index[complexity] += 1
        per_combination_count[tuple(recipe["skills"])] = slot_base + 1

        document = {
            "id": f"Q{number:03d}",
            "question": question,
            "meta": {
                "complexity": complexity,
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

    print(f"added {len(written)} questions (Q{start_number:03d}..Q{start_number + len(written) - 1:03d})")


if __name__ == "__main__":
    main()
