"""Run sampled evaluation questions through the live PEV runtime.

Each question becomes one in-process AgentRun (real DeepSeek model + real
public fetches + SQLite in-memory DB), exactly like the opt-in live E2E test
(tests/integration/test_pev_live_end_to_end.py).  Results are written as one
JSON file per question under ``--out-dir`` for later merging/analysis.

Seeding policy (documented, deterministic):

  - Discovery questions (specific site named, e.g. bytedance/sgcc/cofco) get
    ``candidate_urls`` only when the probe rounds found fetchable pages for
    that site (state-owned announcement aggregators, campus job posts, v2ex).
    Sites whose pages are SPA/walled under the requests-based fetch (bytedance,
    tencent, zhaopin, lagou) get NO seeds: the run must degrade safely, which
    is exactly the behaviour under test.
  - "already-collected" questions (JM/RT/CP only, ``time_window: null`` or
    "已收集" wording) are seeded with role-matched, probe-verified public JD
    pages, because a fresh run has no collected evidence of its own.

Chain questions: a doc with ``{"chain": [doc1, doc2, doc3]}`` (max 3 links)
runs each link as its own PEV run in a fresh session; a link only starts when
the previous link succeeded, and the previous link's collected artifact URLs
are injected as ``candidate_urls`` (plus a ``chain_context`` note quoting the
previous summary). The chain result JSON nests per-link records under
``links`` and reports the chain-level status (stops at the first non-success).

Usage::

    python -m tests.question.eval_runner --ids Q001 Q002 --out-dir tests/question/eval_results/round_1
    python -m tests.question.eval_runner --ids C001 C002 --question-dir tests/question/redesign --out-dir tests/question/eval_results/retry_6
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import (
    AgentArtifact,
    AgentEvent,
    AgentPlan,
    AgentStep,
    AgentTurn,
    User,
    UserRole,
)
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.model_gateway import build_agent_model_gateway
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.provider_config import load_project_env
from backend.app.services.agent_runtime.runtime import AgentRuntime
from backend.app.services.agent_runtime.schemas import AgentBudget, AgentTaskRequest
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent
from backend.app.services.career_skills.registry import build_career_tool_registry
from backend.app.services.profile_parser import extract_evidence_candidates
from tests.conftest import settings_override

QUESTION_DIR = pathlib.Path(__file__).resolve().parent
ALL_SKILLS = ["job-discovery", "job-matching", "resume-tailoring", "career-planning"]
DEFAULT_BUDGET = AgentBudget(
    max_agent_turns=36,
    max_tool_calls=32,
    max_replans=2,
    max_wall_clock_seconds=300,
)

# ---------------------------------------------------------------- seed bank
# Every URL below was probe-verified fetchable by fetch_public_job_pages on
# 2026-08-04 (requests-based, with the character counts shown); iguopin SPA
# search pages (IGUOPIN_SEARCH, render-verified 2026-08-08) are the one
# exception: they need the eval's playwright fallback to yield job cards.

BAIDU_TALENT_URLS = [  # 546-1026ch each, real JD detail pages
    "https://talent.baidu.com/jobs/detail/GRADUATE/4f1cbc80-8332-4a92-b8fa-c0132b17d47e",
    "https://talent.baidu.com/jobs/detail/GRADUATE/74d83772-1bd0-42b9-8cc5-69eb45696b62",
    "https://talent.baidu.com/jobs/detail/SOCIAL/75d3af47-7f79-4d71-862b-6fbca577bb19",
    "https://talent.baidu.com/jobs/detail/GRADUATE/3287bb6a-8c27-4648-a3c2-b3cac16c3d36",
    "https://talent.baidu.com/jobs/detail/GRADUATE/6f9c3a86-6557-409d-8fa7-e6f4c68d6765",
    "https://talent.baidu.com/jobs/detail/SOCIAL/5bb42582-10ab-4f49-94a6-7ee296885d8f",
    "https://talent.baidu.com/jobs/detail/INTERN/cd423c1c-7a35-4672-b0a7-2857308efe43",
]
LIEPIN_ROLE_URLS = {  # role landing pages, 3961-5915ch each with real JD cards
    "java": "https://www.liepin.com/zphouduanjavakaifagongchengshi/",  # 5915ch
    "frontend": "https://www.liepin.com/zpqiandongruanjiankaifagongchengshi/",  # 5523ch
    "aigc": "https://www.liepin.com/zpchanpinjingli/",  # 4979ch (产品经理 incl. AIGC PM)
    "llm-dev": "https://www.liepin.com/zpdmxyykfgcsz24g/",  # 3961ch 大模型应用开发工程师
}
LIEPIN_LLM_JOB = "https://www.liepin.com/job/1974201059.shtml"  # 235ch, 中电兴发
SGCC_EVIDENCE = [  # 国家电网 announcements (955-6084ch)
    "https://www.gwy.com/gqzp/gjdw/",
    "http://sa.offcn.com/tag/220695.html",
    "https://www.51test.net/show/11209793.html",
    "https://www.51test.net/show/11207234.html",
    "http://gz.bendibao.com/news/20241016/content354334.shtml",
]
COFCO_EVIDENCE = [  # 中粮集团 2026 校招 (748-3074ch)
    "https://www.fenbi.com/page/positions-exams/9/548984",
    "https://career.hebut.edu.cn/correcruit/content/id/78016.html",
]
CAMPUS_EVIDENCE = [  # campus sources (1034-3074ch)
    "https://career.hebut.edu.cn/correcruit/content/id/78016.html",
    "https://job.ncss.cn/student/m/index.html",
]
# NOTE: v2ex.com is unreachable from this machine (2026-08-05, ConnectTimeout),
# so V2EX-seeded questions were re-pointed at liepin role landing pages.
# iguopin is a CRA SPA: homepage and search pages return an HTML shell to
# requests and only yield usable text through the eval's playwright fallback
# (render-verified 2026-08-08: /job/list?keyword=* -> real per-job cards with
# 「城市」city lines, e.g. "Java后端开发工程师" -> 18 cards). Seeds point at
# keyword search pages (not the homepage), so the executor sees role-matched
# job cards instead of an announcement stream.
IGUOPIN_SEARCH = {  # https://www.iguopin.com/job/list?keyword=<kw>, URL-encoded
    "java": "https://www.iguopin.com/job/list?keyword=Java%E5%90%8E%E7%AB%AF%E5%BC%80%E5%8F%91%E5%B7%A5%E7%A8%8B%E5%B8%88",  # Java后端开发工程师
    "frontend": "https://www.iguopin.com/job/list?keyword=%E5%89%8D%E7%AB%AF%E5%BC%80%E5%8F%91%E5%B7%A5%E7%A8%8B%E5%B8%88",  # 前端开发工程师
    "ai": "https://www.iguopin.com/job/list?keyword=AI%E7%AE%97%E6%B3%95%E5%B7%A5%E7%A8%8B%E5%B8%88",  # AI算法工程师
    "pm": "https://www.iguopin.com/job/list?keyword=%E4%BA%A7%E5%93%81%E7%BB%8F%E7%90%86",  # 产品经理
}

# question id -> (urls, seed note)
# 2026-08-05 redesigned set: B-class (seed-disconnect) questions deleted
# (Q003/Q004 sgcc, Q083/Q118); Q013 seeds cleaned (liepin llm-dev only).
# Chain links run under their own id "C###-L<n>"; link 1 carries the source
# seeds, links 2+ inherit candidate_urls from the previous link's artifacts.
SEED_URLS: dict[str, tuple[list[str], str]] = {
    "Q011": ([LIEPIN_ROLE_URLS["frontend"]], "liepin 前端 role landing page (5523ch, real JDs)"),
    "Q013": ([LIEPIN_ROLE_URLS["llm-dev"]], "liepin LLM-dev landing page (sgcc seeds removed)"),
    "Q017": ([*BAIDU_TALENT_URLS, CAMPUS_EVIDENCE[0]], "campus GRADUATE JDs + cofco campus post"),
    "Q028": (CAMPUS_EVIDENCE, "campus job pages (probe-verified)"),
    "Q034": ([IGUOPIN_SEARCH["java"]], "iguopin Java 后端搜索页 (render-verified job cards)"),
    "Q040": ([LIEPIN_ROLE_URLS["aigc"]], "liepin 产品经理专区 incl. AIGC 专场"),
    "Q045": ([LIEPIN_ROLE_URLS["llm-dev"]], "liepin 大模型应用开发 role landing page"),
    "Q046": ([LIEPIN_ROLE_URLS["java"]], "liepin 后端 role landing page"),
    "Q055": ([LIEPIN_ROLE_URLS["frontend"]], "liepin 前端 role landing page (aggregator)"),
    "Q148": ([LIEPIN_ROLE_URLS["aigc"]], "liepin 产品经理专区 incl. AIGC 专场"),
    # R001-R010 (台账时间窗) / R011-R014 (大厂 AIGC PM) / R041/R042 (台账):
    # no seeds -- smartsheet query is the primary path; official SPA sites
    # degrade via search (bytedance/tencent), same as Q081/Q103/Q144.
    "R015": ([IGUOPIN_SEARCH["java"]], "iguopin Java 后端搜索页 (render-verified job cards)"),
    "R016": ([IGUOPIN_SEARCH["java"]], "iguopin Java 后端搜索页 (render-verified job cards)"),
    "R017": ([IGUOPIN_SEARCH["frontend"]], "iguopin 前端搜索页 (render-verified job cards)"),
    "R018": ([IGUOPIN_SEARCH["frontend"]], "iguopin 前端搜索页 (render-verified job cards)"),
    "R019": ([IGUOPIN_SEARCH["ai"]], "iguopin AI 算法搜索页 (render-verified job cards)"),
    "R020": ([IGUOPIN_SEARCH["pm"]], "iguopin 产品经理搜索页 (render-verified job cards)"),
    "R021": ([IGUOPIN_SEARCH["java"]], "iguopin Java 后端搜索页 (render-verified job cards)"),
    "R022": ([IGUOPIN_SEARCH["frontend"]], "iguopin 前端搜索页 (render-verified job cards)"),
    "R023": ([LIEPIN_ROLE_URLS["llm-dev"]], "liepin 大模型应用开发 role landing page"),
    "R024": ([LIEPIN_ROLE_URLS["java"]], "liepin 后端 role landing page"),
    "R025": ([LIEPIN_ROLE_URLS["frontend"]], "liepin 前端 role landing page"),
    "R026": ([LIEPIN_ROLE_URLS["aigc"]], "liepin 产品经理专区 incl. AIGC 专场"),
    "R027": ([LIEPIN_ROLE_URLS["java"]], "liepin 后端 role landing page"),
    "R028": ([LIEPIN_ROLE_URLS["frontend"]], "liepin 前端 role landing page"),
    "R029": ([LIEPIN_ROLE_URLS["llm-dev"]], "liepin 大模型应用开发 role landing page"),
    "R030": ([IGUOPIN_SEARCH["java"]], "iguopin Java 后端搜索页 (render-verified job cards)"),
    "R031": ([IGUOPIN_SEARCH["frontend"]], "iguopin 前端搜索页 (render-verified job cards)"),
    "R040": (BAIDU_TALENT_URLS, "baidu talent role-matched JDs"),
    "R043": ([LIEPIN_ROLE_URLS["java"]], "liepin 后端 role landing page"),
    "R044": ([LIEPIN_ROLE_URLS["frontend"]], "liepin 前端 role landing page"),
    "R045": ([LIEPIN_ROLE_URLS["aigc"]], "liepin 产品经理专区 incl. AIGC 专场"),
    "R046": ([LIEPIN_ROLE_URLS["llm-dev"]], "liepin 大模型应用开发 role landing page"),
    "R047": (BAIDU_TALENT_URLS, "baidu talent role-matched JDs"),
    # chains: link 1 source seeds; links 2/3 inherit artifacts (no seeds).
    "C001-L1": ([LIEPIN_ROLE_URLS["llm-dev"]], "liepin 大模型应用开发 role landing page"),
    "C002-L1": (BAIDU_TALENT_URLS, "baidu talent role-matched JDs"),
    "C003-L1": ([LIEPIN_ROLE_URLS["llm-dev"]], "liepin 大模型应用开发 role landing page"),
    "C004-L1": ([LIEPIN_ROLE_URLS["llm-dev"]], "liepin 大模型应用开发 role landing page"),
    "C006-L1": ([LIEPIN_ROLE_URLS["java"]], "liepin 后端 role landing page"),
    "C007-L1": ([LIEPIN_ROLE_URLS["java"]], "liepin 后端 role landing page"),
    "C008-L1": ([LIEPIN_ROLE_URLS["java"]], "liepin 后端 role landing page"),
    "C009-L1": ([LIEPIN_ROLE_URLS["frontend"]], "liepin 前端 role landing page"),
    "C010-L1": ([LIEPIN_ROLE_URLS["frontend"]], "liepin 前端 role landing page"),
    "C011-L1": ([LIEPIN_ROLE_URLS["frontend"]], "liepin 前端 role landing page"),
    "C012-L1": ([LIEPIN_ROLE_URLS["aigc"]], "liepin 产品经理专区 incl. AIGC 专场"),
    "C013-L1": ([LIEPIN_ROLE_URLS["aigc"]], "liepin 产品经理专区 incl. AIGC 专场"),
    "C014-L1": ([LIEPIN_ROLE_URLS["aigc"]], "liepin 产品经理专区 incl. AIGC 专场"),
    "C015-L1": (BAIDU_TALENT_URLS, "baidu talent role-matched JDs"),
    # C005-L1 (台账 3 天) / R032-R034 (juejin) / R035-R039 (官网): no seeds --
    # smartsheet first, or search/degrade under test.
}

# ------------------------------------------------------------- profile facts
def build_profile_facts(profile: dict) -> dict:
    """Render profile.summary into sectioned resume text and extract facts.

    Mirrors the live E2E: confirmed_profile_facts = {field_path: value} built
    from extract_evidence_candidates, so the agents see the same shapes.

    A profile may carry a full ``resume_text`` (real resume with section
    headings the parser knows: 教育经历/实习经历/项目经历/技能/获奖/证书);
    when present it replaces the regex-derived one-liner so tailoring and
    matching agents see the real evidence (education, internship, projects).
    """
    import re

    if profile.get("resume_text"):
        return {
            candidate.field_path: candidate.candidate_value
            for candidate in extract_evidence_candidates(profile["resume_text"])
        }

    summary = profile["summary"]
    skills_match = re.search(r"技能：(.+?)(?:，|$)", summary)
    skills = skills_match.group(1) if skills_match else ""
    exp_match = re.search(r"(应届生（校招）|社招（\d+ 年经验）)", summary)
    exp = exp_match.group(1) if exp_match else ""
    resume_text = "\n".join(
        line for line in (f"{profile['role']}（评测画像）", "教育经历", exp, "技能", skills) if line
    )
    return {
        candidate.field_path: candidate.candidate_value
        for candidate in extract_evidence_candidates(resume_text)
    }


# ------------------------------------------------------------------- runner
def run_question(
    db: Session,
    qid: str,
    doc: dict,
    *,
    budget: AgentBudget,
    extra_context: dict | None = None,
    inherited_evidence: list[dict] | None = None,
) -> dict:
    """Run one question through the PEV runtime and record everything.

    ``extra_context`` lets a chain link inject the previous link's outcome
    (candidate_urls from collected artifacts, plus a chain_context note)
    without changing the question text itself.

    ``inherited_evidence`` seeds the run's ``observed_public_evidence`` with
    tool-produced evidence artifacts collected by an earlier chain link
    (artifact_id/source_url/content_hash/visible_text, capped like the
    runtime's own projection). This is the chained-question contract: link B
    answers using link A's real collected evidence — the same shape the live
    runtime exposes to later Agent turns within one run.
    """
    started = time.monotonic()
    urls, seed_note = SEED_URLS.get(qid, ([], "no seeds (search/degrade under test)"))
    facts = build_profile_facts(doc["profile"])
    context: dict = {}
    if urls:
        context["candidate_urls"] = urls
    if extra_context:
        context.update(extra_context)
    if inherited_evidence:
        remaining_characters = 48_000
        bounded: list[dict] = []
        for item in inherited_evidence:
            text = item.get("visible_text")
            if not isinstance(text, str) or not text:
                continue
            bounded.append({**item, "visible_text": text[:remaining_characters]})
            remaining_characters -= min(len(text), remaining_characters)
            if remaining_characters <= 0:
                break
        context["observed_public_evidence"] = bounded
    task = AgentTaskRequest(
        goal=doc["question"],
        allowed_skills=list(ALL_SKILLS),
        context=context,
        private_context={"confirmed_profile_facts": facts},
        budget=budget,
    )
    user = User(
        id=f"eval-{qid}",
        account=f"{qid}@eval.test",
        nickname=qid,
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db.add(user)
    db.commit()

    settings = settings_override(agent_harness_enabled=True)
    gateway = build_agent_model_gateway(settings)
    tools = build_career_tool_registry()
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=tools),
        executor=ExecutorAgent(gateway=gateway, tools=tools),
        verifier=VerifierAgent(gateway=gateway, tools=tools),
        agent_version="pev-eval",
    )
    result = runtime.run(db, user_id=user.id, task=task)
    wall_seconds = round(time.monotonic() - started, 1)

    run_id = result.run_id
    plans = list(db.scalars(select(AgentPlan).where(AgentPlan.run_id == run_id).order_by(AgentPlan.revision)))
    steps = list(db.scalars(select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.sequence)))
    # The planned steps come from the persisted plan (authoritative), not the
    # AgentStep table: a run that stops inside step 1 must still show the full
    # multi-step plan the Planner produced. AgentStep rows only add status.
    final_plan_steps = (
        plans[-1].plan_json.get("steps", []) if plans else []
    )
    executed_by_sequence = {step.sequence: step for step in steps}
    # Chronological by insert id, not turn_index: per-role indices make the
    # verifier's decision look interleaved with the executor's turns.
    turns = list(db.scalars(select(AgentTurn).where(AgentTurn.run_id == run_id).order_by(AgentTurn.id)))
    artifacts = list(db.scalars(select(AgentArtifact).where(AgentArtifact.run_id == run_id)))
    events = list(db.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id).order_by(AgentEvent.sequence)))

    tool_calls: dict[str, dict] = {}
    for event in events:
        payload = event.payload_json or {}
        tool_name = payload.get("tool")
        if not tool_name:
            continue
        bucket = tool_calls.setdefault(
            tool_name, {"tool_name": tool_name, "succeeded": 0, "failed": 0, "error_codes": []}
        )
        if event.event_type == "executor_tool_failed":
            bucket["failed"] += 1
            code = payload.get("error_code")
            if code and code not in bucket["error_codes"]:
                bucket["error_codes"].append(code)
        elif event.event_type in {
            "executor_tool_observation",
            "executor_structured_artifact",
            "executor_skill_artifact",
        }:
            bucket["succeeded"] += 1

    verifier_decisions: list[str] = []
    turn_summaries: list[dict] = []
    for turn in turns:
        decision = turn.decision_json or {}
        turn_summaries.append(
            {
                "role": turn.role.value,
                "turn_index": turn.turn_index,
                "action": decision.get("action"),
                "tool_name": decision.get("tool_name"),
                "verification_decision": decision.get("verification_decision"),
                "status": decision.get("status"),
                "error_code": decision.get("error_code"),
                "summary": (decision.get("summary") or decision.get("feedback") or "")[:400],
            }
        )
        if decision.get("verification_decision"):
            verifier_decisions.append(decision["verification_decision"])

    return {
        "id": qid,
        "run_id": run_id,
        "question": doc["question"],
        "meta": doc["meta"],
        "profile_id": doc["profile"]["id"],
        "seeded_urls": urls,
        "seed_note": seed_note,
        "injected_context": extra_context,
        "confirmed_facts": facts,
        "result": {
            "status": result.status.value,
            "error_code": result.error_code,
            "summary": result.summary,
        },
        "plan": {
            "revisions": len(plans),
            "final_complexity": plans[-1].complexity.value if plans else None,
            "steps": [
                {
                    "sequence": index + 1,
                    "objective": planned.get("objective"),
                    "allowed_skills": planned.get("allowed_skills"),
                    "status": (
                        executed_by_sequence[index + 1].status.value
                        if index + 1 in executed_by_sequence
                        else "not_started"
                    ),
                    "error_code": (
                        executed_by_sequence[index + 1].error_code
                        if index + 1 in executed_by_sequence
                        else None
                    ),
                }
                for index, planned in enumerate(final_plan_steps)
            ],
        },
        "turns": turn_summaries,
        "verifier_decisions": verifier_decisions,
        "artifacts": [
            {
                "artifact_type": artifact.artifact_type,
                "source_url": artifact.source_url,
                "created_by": artifact.created_by.value,
            }
            for artifact in artifacts
        ],
        "tool_calls": sorted(tool_calls.values(), key=lambda item: item["tool_name"]),
        "wall_seconds": wall_seconds,
        "input_tokens": sum(turn.input_tokens or 0 for turn in turns),
        "output_tokens": sum(turn.output_tokens or 0 for turn in turns),
    }


def run_chain(db: Session, cid: str, doc: dict, *, budget: AgentBudget) -> dict:
    """Run a chained question: link N+1 only runs when link N succeeded.

    Each link is one full PEV run in a fresh session. A succeeded link's
    collected artifacts (source_urls) become the next link's candidate_urls,
    plus a chain_context note quoting the previous link's summary — the
    question chain answers with real evidence instead of a fresh session
    pretending earlier steps happened. The chain stops at the first
    non-succeeded link (per the chained-question contract: 问题A回答完后
    (success后)再次回答问题B).
    """
    started = time.monotonic()
    links = doc["chain"]
    if not isinstance(links, list) or not links:
        raise ValueError(f"{cid}: 'chain' must be a non-empty list of question docs")
    records: list[dict] = []
    for index, link_doc in enumerate(links, start=1):
        link_id = f"{cid}-L{index}"
        extra_context: dict | None = None
        if records:
            prev = records[-1]
            prev_urls = [
                artifact["source_url"]
                for artifact in prev["artifacts"]
                if artifact.get("source_url")
            ]
            if prev_urls:
                extra_context = {"candidate_urls": prev_urls}
            summary = (prev["result"].get("summary") or "").strip()
            if summary:
                # Deliberately do NOT quote the previous link's artifact ids:
                # this link is a fresh session with no persisted evidence, so
                # the executor must re-capture the candidate URLs itself.
                note = (
                    f"上一环节（{prev['id']}）已完成岗位收集，但本环节是全新会话，"
                    f"上一环节的证据工件在当前会话中不存在，必须基于 "
                    f"candidate_urls 中的 URL 重新抓取岗位页面获取 JD 证据后，"
                    f"才能进行本环节的任务。上一环节成果参考：{summary[:200]}"
                )
                extra_context = {
                    **(extra_context or {}),
                    "chain_context": note,
                }
            # The previous link's URLs become this link's candidate_urls so the
            # executor re-captures them itself: the harness only rebuilds
            # observed_public_evidence from THIS run's persisted artifacts
            # between steps (runtime._with_observed_public_evidence), so
            # in-memory evidence injected from an earlier run would be wiped
            # before any step after the first. Fetching candidates inside the
            # link persists evidence rows that survive step boundaries.
        record = run_question(
            db,
            link_id,
            link_doc,
            budget=budget,
            extra_context=extra_context,
        )
        records.append(record)
        if record["result"]["status"] != "succeeded":
            break
    last = records[-1]
    return {
        "id": cid,
        "type": "chain",
        "chain_length": len(links),
        "links": records,
        "result": {
            "status": last["result"]["status"],
            "error_code": last["result"]["error_code"],
            "summary": last["result"]["summary"],
        },
        "wall_seconds": round(time.monotonic() - started, 1),
        "input_tokens": sum(r["input_tokens"] for r in records),
        "output_tokens": sum(r["output_tokens"] for r in records),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="+", required=True, help="question ids, e.g. Q001 Q002")
    parser.add_argument("--out-dir", required=True, help="directory for per-question JSON results")
    parser.add_argument(
        "--question-dir",
        default=None,
        help="directory holding Q###.json / C###.json docs (default: tests/question)",
    )
    args = parser.parse_args()

    load_project_env()
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    question_dir = pathlib.Path(args.question_dir) if args.question_dir else QUESTION_DIR
    # Real eval exercises the full evidence path: allow fetch-public-job-pages
    # to fall back to a headless-Chromium render when the requests fast path
    # returns an empty SPA/login shell (mirrors runtime assembly in main.py).
    from backend.app.services.career_skills import job_discovery as jd_skill

    jd_skill.enable_playwright_fallback(True)

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    try:
        for qid in args.ids:
            doc_path = question_dir / f"{qid}.json"
            if not doc_path.exists():
                print(f"SKIP {qid}: {doc_path.name} missing")
                continue
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
            if "chain" in doc:
                print(f"RUN {qid}: chain of {len(doc['chain'])} links", flush=True)
                record = run_chain(db, qid, doc, budget=DEFAULT_BUDGET)
            else:
                print(f"RUN {qid}: {doc['question'][:60]}...", flush=True)
                record = run_question(db, qid, doc, budget=DEFAULT_BUDGET)
            target = out_dir / f"{qid}.json"
            target.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            turns = (
                sum(len(link["turns"]) for link in record["links"])
                if "links" in record
                else len(record["turns"])
            )
            print(
                f"DONE {qid}: status={record['result']['status']} "
                f"error={record['result']['error_code']} "
                f"wall={record['wall_seconds']}s turns={turns}",
                flush=True,
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
