"""Diagnostic: run the FULL PATH C supervisor on xiaomi with a high recursion
limit and dump the tool-call sequence, so we can see WHY it loops to the
recursion limit (instead of converging in 3 calls as the prompt mandates).

run_web_navigation alone returns 138 candidates with no crash (confirmed by
_diag_supervisor_xiaomi.py), so the loop is in the supervisor LLM, not capture.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("RUN_SUPERVISOR_BASELINE", "1")

from langchain_core.messages import HumanMessage  # noqa: E402

from backend.app.config import Settings  # noqa: E402
from backend.app.db.base import Base  # noqa: E402
from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _build_job_discovery_llm,
    build_discovery_supervisor_agent,
    invoke_supervisor_agent,
)
from backend.app.services.job_discovery.result_contract import (  # noqa: E402
    parse_agent_result,
)
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput  # noqa: E402
from backend.app.services.job_discovery.normalization.jd_normalizer import (  # noqa: E402
    normalize_title,
)
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from dataclasses import asdict  # noqa: E402
import hashlib  # noqa: E402

URL = "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY"
COMPANY = "小米"
RECURSION_LIMIT = 80  # high, to see if it converges at all


def _settings() -> Settings:
    return Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=900,
        job_discovery_max_pages_per_task=30,
        job_discovery_ocr_enabled=True,
        job_discovery_strategy_enabled=True,
    )


def _tool_name(msg) -> str:
    name = getattr(msg, "name", None)
    if name:
        return name
    tcs = getattr(msg, "tool_calls", None)
    if tcs:
        return ",".join(tc.get("name", "?") for tc in tcs)
    return type(msg).__name__


def main() -> None:
    settings = _settings()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    llm = _build_job_discovery_llm(settings=settings)

    url_hash = hashlib.sha256(URL.encode()).hexdigest()[:16]
    task_input = DiscoveryTaskInput(
        source_id="baseline", raw_record_id=f"base-{url_hash}",
        external_record_id=f"base-{url_hash}", source_key="baseline",
        source_url=URL, url_hash=url_hash, record_fields=[],
    )
    agent = build_discovery_supervisor_agent(
        settings=settings, model=llm, snapshot_context=None)
    msg = json.dumps(asdict(task_input), ensure_ascii=False)
    agent_input = {"messages": [HumanMessage(content=msg)]}

    print(f"invoking supervisor (recursion_limit={RECURSION_LIMIT})...", flush=True)
    try:
        raw = invoke_supervisor_agent(
            agent, agent_input, config={"recursion_limit": RECURSION_LIMIT})
    except Exception as exc:  # noqa: BLE001
        print(f"supervisor CRASHED: {exc!r}", flush=True)
        return

    messages = raw.get("messages", []) if isinstance(raw, dict) else []
    print(f"\n--- {len(messages)} messages ---", flush=True)
    for i, m in enumerate(messages):
        name = _tool_name(m)
        content = getattr(m, "content", "")
        clen = len(content) if isinstance(content, str) else 0
        print(f"  [{i:2d}] {name}  (content_len={clen})", flush=True)

    # Final structured_response?
    sr = raw.get("structured_response") if isinstance(raw, dict) else None
    print(f"\nstructured_response present: {sr is not None}", flush=True)
    if sr:
        print(f"  status={sr.get('status')} cand_count={len(sr.get('candidates') or [])}",
              flush=True)

    try:
        result = parse_agent_result(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"parse_agent_result failed: {exc!r}", flush=True)
        return
    cands = result.candidates or []
    titles = [getattr(c, "title", None) for c in cands]
    uniq = len({normalize_title(t or "") for t in titles})
    print(f"\nparse_agent_result: status={result.status} raw={len(cands)} unique={uniq}",
          flush=True)
    print(f"summary: {(result.summary or '')[:400]}", flush=True)


if __name__ == "__main__":
    main()
