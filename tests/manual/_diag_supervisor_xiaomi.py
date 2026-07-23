"""Diagnostic: call run_web_navigation(xiaomi) directly to see why the
supervisor path returns 0 candidates / hits recursion limit.

Prints: baseline-only candidate count, run_web_navigation evidence_pages /
candidates / error / page_count, and whether it CRASHES (which would explain
the supervisor looping + GraphRecursionError).
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

from backend.app.config import Settings
from backend.app.services.job_discovery.deepagents_runner import (
    _build_job_discovery_llm,
    create_web_navigation_subagent,
    extract_rendered_job_evidence,
    run_web_navigation,
)
from backend.app.services.job_discovery.normalization.jd_normalizer import (
    normalize_title,
)

URL = "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY"


def _settings() -> Settings:
    return Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=480,
        job_discovery_max_pages_per_task=30,
        job_discovery_ocr_enabled=True,
        job_discovery_strategy_enabled=True,
    )


def main() -> None:
    settings = _settings()
    model = _build_job_discovery_llm(settings=settings)

    print("=== baseline extract_rendered_job_evidence ===", flush=True)
    try:
        raw = extract_rendered_job_evidence(URL)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        ev = (parsed or {}).get("evidence_pages") or []
        print(f"baseline evidence_pages={len(ev)}", flush=True)
        by_type: dict[str, int] = {}
        for p in ev:
            t = (p or {}).get("evidence_type") or "?"
            by_type[t] = by_type.get(t, 0) + 1
        print(f"baseline by_type={by_type}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"baseline CRASHED: {exc!r}", flush=True)

    print("\n=== run_web_navigation (baseline + Web Nav Agent) ===", flush=True)
    subagent = create_web_navigation_subagent(settings)
    try:
        res = run_web_navigation(URL, settings=settings, subagent=subagent, model=model)
    except Exception as exc:  # noqa: BLE001
        print(f"run_web_navigation CRASHED: {exc!r}", flush=True)
        return
    if not isinstance(res, dict):
        print(f"run_web_navigation returned non-dict: {type(res)}", flush=True)
        return
    ev_pages = res.get("evidence_pages") or []
    cands = res.get("candidates") or []
    err = res.get("error")
    nav_path = res.get("navigation_path") or []
    page_count = res.get("page_count")
    delegated = res.get("delegated_to")

    def _cand_title(c) -> str:
        if isinstance(c, dict):
            return c.get("title") or ""
        return getattr(c, "title", "") or ""

    uniq = len({normalize_title(_cand_title(c)) for c in cands})
    print(f"evidence_pages={len(ev_pages)}", flush=True)
    print(f"candidates raw={len(cands)} unique={uniq}", flush=True)
    print(f"error={err!r}", flush=True)
    print(f"page_count={page_count} delegated_to={delegated} "
          f"nav_path_len={len(nav_path)}", flush=True)
    ev_by_type: dict[str, int] = {}
    for p in ev_pages:
        t = (p or {}).get("evidence_type") or "?"
        ev_by_type[t] = ev_by_type.get(t, 0) + 1
    print(f"evidence by_type={ev_by_type}", flush=True)
    titles = [_cand_title(c) for c in cands][:12]
    print(f"first 12 titles: {titles}", flush=True)
    for i, p in enumerate(ev_pages[:4]):
        if not isinstance(p, dict):
            continue
        txt = (p.get("text_excerpt") or "")
        print(f"  ev[{i}] type={p.get('evidence_type')} "
              f"url={p.get('url')} text_head={txt[:200]!r}", flush=True)


if __name__ == "__main__":
    main()
