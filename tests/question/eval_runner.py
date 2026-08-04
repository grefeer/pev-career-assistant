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

Usage::

    python -m tests.question.eval_runner --ids Q001 Q002 --out-dir tests/question/eval_results/round_1
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
from backend.app.domain.agent_runtime import AgentRole, RunStatus
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
# Every URL below was probe-verified fetchable by fetch_public_job_pages
# (requests-based) on 2026-08-04, with the character counts shown.

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
V2EX_JOBS = "https://www.v2ex.com/go/jobs"  # 2168ch, tech-vertical

# question id -> (urls, seed note)
SEED_URLS: dict[str, tuple[list[str], str]] = {
    "Q003": (SGCC_EVIDENCE, "sgcc announcement pages (probe-verified)"),
    "Q004": (SGCC_EVIDENCE, "sgcc announcement pages (probe-verified)"),
    "Q007": (CAMPUS_EVIDENCE, "campus job pages (probe-verified)"),
    "Q009": ([V2EX_JOBS, *BAIDU_TALENT_URLS], "tech-vertical v2ex + role-matched baidu JDs"),
    "Q011": ([LIEPIN_ROLE_URLS["frontend"]], "liepin 前端 role landing page (5523ch, real JDs)"),
    "Q013": ([*SGCC_EVIDENCE, LIEPIN_ROLE_URLS["llm-dev"]], "sgcc announcements + liepin LLM-dev landing page"),
    "Q014": (COFCO_EVIDENCE, "cofco 2026 campus pages (probe-verified)"),
    "Q017": ([*BAIDU_TALENT_URLS, CAMPUS_EVIDENCE[0]], "campus GRADUATE JDs + cofco campus post"),
    "Q018": ([*BAIDU_TALENT_URLS, CAMPUS_EVIDENCE[0], LIEPIN_ROLE_URLS["java"]], "campus + role-matched Java JDs"),
    "Q019": ([V2EX_JOBS], "tech-vertical v2ex jobs"),
    "Q028": (CAMPUS_EVIDENCE, "campus job pages (probe-verified)"),
    "Q050": ([V2EX_JOBS, LIEPIN_ROLE_URLS["java"]], "tech-vertical + role-matched Java landing page"),
    "Q055": ([LIEPIN_ROLE_URLS["frontend"]], "liepin 前端 role landing page (aggregator)"),
    "Q060": ([V2EX_JOBS, LIEPIN_ROLE_URLS["aigc"], LIEPIN_ROLE_URLS["llm-dev"]], "tech-vertical + 产品经理/AIGC landing pages"),
    # Q001/Q002 (bytedance), Q005/Q006 (zhaopin), Q016 (lagou), Q032 (tencent):
    # no seeds -- SPA or gated under requests fetch; safe degradation is the behaviour under test.
}

# ------------------------------------------------------------- profile facts
def build_profile_facts(profile: dict) -> dict:
    """Render profile.summary into sectioned resume text and extract facts.

    Mirrors the live E2E: confirmed_profile_facts = {field_path: value} built
    from extract_evidence_candidates, so the agents see the same shapes.
    """
    import re

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
def run_question(db: Session, qid: str, doc: dict, *, budget: AgentBudget) -> dict:
    """Run one question through the PEV runtime and record everything."""
    started = time.monotonic()
    urls, seed_note = SEED_URLS.get(qid, ([], "no seeds (search/degrade under test)"))
    facts = build_profile_facts(doc["profile"])
    task = AgentTaskRequest(
        goal=doc["question"],
        allowed_skills=list(ALL_SKILLS),
        context={"candidate_urls": urls} if urls else {},
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
        "question": doc["question"],
        "meta": doc["meta"],
        "profile_id": doc["profile"]["id"],
        "seeded_urls": urls,
        "seed_note": seed_note,
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="+", required=True, help="question ids, e.g. Q001 Q002")
    parser.add_argument("--out-dir", required=True, help="directory for per-question JSON results")
    args = parser.parse_args()

    load_project_env()
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    try:
        for qid in args.ids:
            doc_path = QUESTION_DIR / f"{qid}.json"
            if not doc_path.exists():
                print(f"SKIP {qid}: {doc_path.name} missing")
                continue
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
            print(f"RUN {qid}: {doc['question'][:60]}...", flush=True)
            record = run_question(db, qid, doc, budget=DEFAULT_BUDGET)
            target = out_dir / f"{qid}.json"
            target.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(
                f"DONE {qid}: status={record['result']['status']} "
                f"error={record['result']['error_code']} "
                f"wall={record['wall_seconds']}s turns={len(record['turns'])}",
                flush=True,
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
