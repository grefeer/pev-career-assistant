"""Job Discovery Worker — polls the task queue and processes discovery tasks.

Usage:
    worker = JobDiscoveryWorker(SessionLocal, get_settings())
    worker.run_once()        # process a single task
    worker.run_loop()        # poll forever
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from dataclasses import asdict
from typing import Any, Callable

import yaml
from langchain_core.messages import HumanMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.models import (
    DiscoveryBlockReason,
    JobDiscoveryTask,
    RawJobRecord,
)
from backend.app.repositories.job_discovery import (
    claim_next_task,
    mark_task_failed,
    mark_task_needs_manual_review,
    mark_task_partial_success,
    mark_task_succeeded,
    upsert_candidate,
    upsert_evidence,
)
from backend.app.services.job_discovery.deepagents_runner import (
    build_crawl_plan_agent,
    build_discovery_supervisor_agent,
    create_web_navigation_subagent,
    package_candidates,
    run_web_navigation,
    standardize_from_record_fields,
    verify_evidence,
    _build_job_discovery_llm,
)
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    DiscoveryTaskInput,
    NormalizedJobCandidate,
    PageEvidence,
)
from backend.app.services.job_discovery.result_contract import (
    AgentResultParseError,
    enforce_result_invariants,
    parse_agent_result,
)
from backend.app.services.job_discovery.planning.crawl_plan_agent import (
    generate_crawl_plan,
    repair_crawl_plan,
)
from backend.app.services.job_discovery.crawling.coverage import verify_coverage
from backend.app.services.job_discovery.crawling.checkpoint import CrawlCheckpoint
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.playwright_driver import (
    PlaywrightCrawlDriver,
)
from backend.app.services.job_discovery.crawling.driver import CrawlDriver
from backend.app.services.job_discovery.adapters.complete_crawl_base import (
    CompleteCrawlAdapter,
)
from backend.app.services.job_discovery.tools import (
    build_candidate_idempotency_key,
    build_similarity_group_key,
)
from backend.app.services.job_discovery.strategy.strategy_router import StrategyRouter
from backend.app.services.job_discovery.strategy.snapshot_executor import (
    SnapshotExecutor,
    SnapshotExecutionResult,
)
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer
from backend.app.services.job_discovery.strategy.trajectory_store import (
    purge_old_trajectories,
    save_trajectory,
    schedule_annotation,
)
from backend.app.services.job_discovery.strategy import strategy_store as strat_store
from backend.app.services.job_discovery.schemas import StrategyRecord
from backend.app.services.job_discovery.strategy import error_classifier


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_worker_id() -> str:
    """Build a unique worker identifier from hostname and PID."""
    return f"{socket.gethostname()}::{os.getpid()}"


def _parse_agent_result(result_raw: Any) -> DiscoveryRunResult:
    """Compatibility wrapper for the shared discovery-result parser."""
    try:
        return parse_agent_result(result_raw)
    except AgentResultParseError:
        return DiscoveryRunResult(
            status="failed",
            block_reason="parse_failed",
            summary="Could not parse structured output from agent result",
        )


def _persist_evidence(
    db: Session,
    task: JobDiscoveryTask,
    evidence_list: list[dict[str, Any]] | list[PageEvidence],
) -> None:
    """Persist a list of evidence items for a task.

    Accepts either ``PageEvidence`` dataclass instances or plain dicts
    (as produced by the agent's structured output).
    """
    for ev in evidence_list:
        if isinstance(ev, dict):
            upsert_evidence(
                db,
                task_id=task.id,
                evidence_type=ev.get("evidence_type", ""),
                url=ev.get("url"),
                title=ev.get("title"),
                content_hash=ev.get("content_hash", ""),
                text_excerpt=ev.get("text_excerpt"),
                storage_uri=ev.get("storage_uri"),
                metadata_json=ev.get("metadata"),
            )
        else:
            upsert_evidence(
                db,
                task_id=task.id,
                evidence_type=ev.evidence_type,
                url=ev.url,
                title=ev.title,
                content_hash=ev.content_hash,
                text_excerpt=ev.text_excerpt,
                storage_uri=None,
                metadata_json=ev.metadata,
            )


def _persist_candidates(
    db: Session,
    task: JobDiscoveryTask,
    candidates_list: list[dict[str, Any]] | list[NormalizedJobCandidate],
) -> None:
    """Persist a list of candidates for a task.

    Accepts either ``NormalizedJobCandidate`` dataclass instances or plain
    dicts (as produced by the agent's structured output / ``package_candidates``).
    """
    for cand in candidates_list:
        if isinstance(cand, dict):
            title = cand.get("title")
            company_name = cand.get("company_name")
            locations = cand.get("locations") or []
            recruitment_types = cand.get("recruitment_types") or []
            evidence_refs = cand.get("evidence_refs") or []
            evidence_hash = _candidate_evidence_hash(evidence_refs, task.url_hash)
            primary_location = locations[0] if locations else ""
            primary_recruitment_type = recruitment_types[0] if recruitment_types else ""
            idempotency_key = cand.get("idempotency_key") or build_candidate_idempotency_key(
                company=company_name or "",
                title=title or "",
                location=primary_location,
                apply_url=cand.get("apply_url") or "",
                evidence_hash=evidence_hash,
            )
            similarity_group_key = cand.get("similarity_group_key") or build_similarity_group_key(
                company=company_name or "",
                title=title or "",
                recruitment_type=primary_recruitment_type,
                source_family=task.source_key,
            )
            upsert_candidate(
                db,
                task_id=task.id,
                source_id=cand.get("source_id", task.source_id),
                raw_record_id=cand.get("raw_record_id", task.raw_record_id),
                external_record_id=cand.get("external_record_id", task.external_record_id),
                idempotency_key=idempotency_key,
                similarity_group_key=similarity_group_key,
                title=title,
                company_name=company_name,
                department=cand.get("department"),
                description_text=cand.get("description_text"),
                responsibilities=cand.get("responsibilities"),
                requirements=cand.get("requirements"),
                locations_json=locations,
                recruitment_types_json=recruitment_types,
                industries_json=cand.get("industries"),
                apply_url=cand.get("apply_url"),
                application_channel_json=cand.get("application_channel_json"),
                deadline_text=cand.get("deadline_text"),
                referral_code=cand.get("referral_code"),
                confidence=cand.get("confidence"),
                evidence_refs_json=evidence_refs,
                normalization_warnings_json=cand.get("normalization_warnings"),
            )
        else:
            evidence_hash = _candidate_evidence_hash(cand.evidence_refs, task.url_hash)
            primary_location = cand.locations[0] if cand.locations else ""
            primary_recruitment_type = cand.recruitment_types[0] if cand.recruitment_types else ""
            upsert_candidate(
                db,
                task_id=task.id,
                source_id=task.source_id,
                raw_record_id=task.raw_record_id,
                external_record_id=task.external_record_id,
                idempotency_key=build_candidate_idempotency_key(
                    company=cand.company_name or "",
                    title=cand.title or "",
                    location=primary_location,
                    apply_url=cand.apply_url or "",
                    evidence_hash=evidence_hash,
                ),
                similarity_group_key=build_similarity_group_key(
                    company=cand.company_name or "",
                    title=cand.title or "",
                    recruitment_type=primary_recruitment_type,
                    source_family=task.source_key,
                ),
                title=cand.title,
                company_name=cand.company_name,
                department=cand.department,
                description_text=cand.description_text or None,
                responsibilities=cand.responsibilities or None,
                requirements=cand.requirements or None,
                locations_json=cand.locations if cand.locations else None,
                recruitment_types_json=cand.recruitment_types if cand.recruitment_types else None,
                industries_json=cand.industries if cand.industries else None,
                apply_url=cand.apply_url,
                application_channel_json=cand.application_channel_json,
                deadline_text=cand.deadline_text,
                referral_code=cand.referral_code,
                confidence=cand.confidence,
                evidence_refs_json=cand.evidence_refs if cand.evidence_refs else None,
                normalization_warnings_json=cand.normalization_warnings if cand.normalization_warnings else None,
            )


def _candidate_evidence_hash(
    evidence_refs: list[dict[str, Any]] | None,
    fallback: str,
) -> str:
    if evidence_refs:
        for ref in evidence_refs:
            value = ref.get("content_hash") or ref.get("hash")
            if isinstance(value, str) and value:
                return value
    return fallback


def _fallback_with_record_fields_if_agent_missed_evidence(
    result: DiscoveryRunResult,
    *,
    task: JobDiscoveryTask,
    task_input: DiscoveryTaskInput,
    settings: Settings,
) -> DiscoveryRunResult:
    """Recover when the LLM agent gives up despite readable public evidence."""
    if not task.source_key.startswith("tencent-"):
        return result
    if not settings.job_discovery_legacy_path_c_enabled:
        return result
    if result.status == "needs_manual_review":
        return result
    if result.execution_error:
        classification = error_classifier.classify_execution_error(result.execution_error)
        if classification.error_type == "blocked":
            return result
    if result.candidates and result.evidence:
        return result
    navigation = run_web_navigation(task.source_url, settings=settings)
    evidence = navigation.get("evidence_pages") or []
    if not evidence:
        return result
    candidates_json = standardize_from_record_fields(
        json.dumps(task_input.record_fields, ensure_ascii=False),
        json.dumps(evidence, ensure_ascii=False),
        task.source_url,
    )
    verified_json = verify_evidence(
        candidates_json,
        json.dumps(evidence, ensure_ascii=False),
    )
    evidence_hash = evidence[0].get("content_hash") or task.url_hash
    candidates = json.loads(
        package_candidates(verified_json, evidence_hash, task.source_key)
    )
    if not candidates:
        return result
    return DiscoveryRunResult(
        status="succeeded",
        evidence=evidence,
        candidates=candidates,
        summary=(
            "Agent returned no candidates; deterministic record-field fallback "
            f"produced {len(candidates)} candidate(s)"
        ),
    )


def _extract_url_pattern(url: str) -> str | None:
    """Derive a simple URL pattern from a URL for trajectory grouping."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        host = parts.netloc
        if not host:
            return None
        path_parts = parts.path.strip("/").split("/")
        if path_parts and path_parts[0]:
            return f"{host}/{path_parts[0]}/*"
        return f"{host}/*"
    except Exception:
        return None


def _load_adapter(adapter_path: str):
    """Dynamically load a DomainAdapter class from a dotted path.

    Example: 'backend.app.services.job_discovery.adapters.alibaba_spa.AlibabaSPAAdapter'
    """
    import importlib
    module_path, class_name = adapter_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)()


def _make_run_web_navigation_wrapper(
    settings: Settings,
    subagent: Any,
    model: Any,
) -> Callable[[str], dict[str, Any]]:
    """Create a run_web_navigation closure with pre-bound dependencies."""
    def _wrapper(start_url: str) -> dict[str, Any]:
        return run_web_navigation(start_url, settings=settings, subagent=subagent, model=model)
    _wrapper.__name__ = "run_web_navigation"  # type: ignore[attr-defined]
    _wrapper.__doc__ = run_web_navigation.__doc__
    _wrapper.__annotations__ = {"start_url": str, "return": dict[str, Any]}  # type: ignore[attr-defined]
    return _wrapper


def _derive_health_check_url(url_pattern: str) -> str:
    """Derive a health-check URL from a strategy URL pattern."""
    clean = url_pattern.replace("*", "").rstrip("/")
    if not clean:
        return ""
    if not clean.startswith("http"):
        clean = "https://" + clean
    return clean


def _snapshot_uses_runtime_navigation(plan_yaml: str) -> bool:
    """Whether a legacy SnapshotPlan needs worker-only navigation dependencies."""
    try:
        raw = yaml.safe_load(plan_yaml)
    except yaml.YAMLError:
        return False
    steps = raw.get("plan", raw) if isinstance(raw, dict) else raw
    return isinstance(steps, list) and any(
        isinstance(step, dict) and step.get("tool") == "run_web_navigation"
        for step in steps
    )


def _declares_crawl_plan(plan_yaml: str) -> bool:
    """Recognize PATH B before validating the planner-owned declaration."""
    try:
        raw = yaml.safe_load(plan_yaml)
    except yaml.YAMLError:
        return False
    return isinstance(raw, dict) and raw.get("plan_type") == "crawl_plan"


def _build_playwright_crawl_driver(settings: Settings) -> PlaywrightCrawlDriver:
    """Create one real, task-scoped Playwright page and its cleanup hook."""
    playwright: Any | None = None
    browser: Any | None = None
    try:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=settings.job_discovery_browser_headless
        )
        page = browser.new_page()
    except Exception as exc:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass
        raise RuntimeError("playwright_crawl_initialization_failed") from exc

    def _close() -> None:
        try:
            page.close()
        finally:
            try:
                browser.close()
            finally:
                playwright.stop()

    return PlaywrightCrawlDriver(page=page, close_callback=_close)


def _planner_enabled(settings: Settings) -> bool:
    """PATH C planning is opt-in only when both gray-migration flags are on."""
    return settings.job_discovery_pev_enabled and settings.job_discovery_planner_enabled


def _crawl_plan_yaml(plan: CrawlPlan) -> str:
    """Serialize a validated plan for the existing deterministic PATH B API."""
    data = asdict(plan)
    data["plan_type"] = "crawl_plan"
    data["pagination"]["type"] = plan.pagination.type.value
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _checkpoint_from_snapshot(snapshot_context: dict | None) -> CrawlCheckpoint | None:
    if not snapshot_context:
        return None
    checkpoint = snapshot_context.get("checkpoint")
    if not isinstance(checkpoint, dict):
        return None
    try:
        return CrawlCheckpoint.from_dict(checkpoint)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class JobDiscoveryWorker:
    """Polls the ``job_discovery_tasks`` queue and processes tasks via the agent.

    Each task is claimed with a lease, processed by the Discovery Supervisor
    Agent, and the result is persisted (evidence, candidates) before the
    lease is released.

    Parameters
    ----------
    db_factory:
        A callable that returns a new SQLAlchemy ``Session``.
    settings:
        Application settings (provides lease timeout, agent model, etc.).
    """

    def __init__(
        self,
        db_factory: Callable[[], Session],
        settings: Settings,
        crawl_driver_factory: Callable[[CrawlPlan, DiscoveryTaskInput], CrawlDriver]
        | None = None,
    ) -> None:
        self.db_factory = db_factory
        self.settings = settings
        self.worker_id = _build_worker_id()
        self._idle_cycles: int = 0
        self._crawl_driver_factory = crawl_driver_factory

    def _execute_planned_crawl(
        self,
        task: DiscoveryTaskInput,
        plan: CrawlPlan,
        trajectory: TrajectoryBuffer,
        snapshot_context: dict | None = None,
    ) -> DiscoveryRunResult:
        """Run a PATH C plan through the pre-existing deterministic PATH B."""
        generated_strategy = StrategyRecord(
            id=f"planner:{task.url_hash}",
            url_pattern=task.source_url,
            site_type="planner_generated",
            plan_yaml=_crawl_plan_yaml(plan),
        )
        crawl_driver_factory = self._crawl_driver_factory or (
            lambda _plan, _task: _build_playwright_crawl_driver(self.settings)
        )
        return SnapshotExecutor(
            generated_strategy,
            task,
            trajectory,
            crawl_driver_factory=crawl_driver_factory,
            checkpoint=_checkpoint_from_snapshot(snapshot_context),
        ).execute()

    def _planner_failure_result(self, exc: Exception) -> DiscoveryRunResult | None:
        """Keep blocked/completion outcomes out of every legacy fallback path."""
        classification = error_classifier.classify_execution_error(str(exc))
        if error_classifier.classify_next_action(classification.error_type) == "needs_manual_review":
            return DiscoveryRunResult(
                status="needs_manual_review",
                block_reason=classification.reason,
                summary=f"Planner requires manual review: {classification.reason}",
            )
        return None

    def _execution_failure_context(
        self,
        result: DiscoveryRunResult,
        snapshot_context: dict | None,
    ) -> dict | None:
        """Build one deterministic error source without inspecting result data."""
        failed_step = (snapshot_context or {}).get("failed_step")
        failed_step = failed_step if isinstance(failed_step, dict) else {}
        raw_error = (
            result.execution_error
            or getattr(result, "error", None)
            or result.block_reason
            or failed_step.get("error")
        )
        if not isinstance(raw_error, str) or not raw_error:
            return None
        context = dict(snapshot_context or {})
        context["failed_step"] = dict(failed_step)
        context["raw_error"] = raw_error
        return context

    def _classify_execution_context(
        self,
        snapshot_context: dict | None,
    ) -> error_classifier.ExecutionErrorClassification | None:
        """Classify raw failure text before considering a declared wrapper type."""
        context = snapshot_context or {}
        raw_error = context.get("raw_error")
        raw_classification = error_classifier.classify_execution_error(
            raw_error if isinstance(raw_error, str) else ""
        )
        if raw_classification.reason != "unknown":
            return raw_classification

        failed_step = context.get("failed_step")
        declared_type = (
            failed_step.get("error_type") if isinstance(failed_step, dict) else None
        )
        if declared_type in {
            "structure_error",
            "transient",
            "blocked",
            "completion_unverified",
            "data_error",
        }:
            return error_classifier.ExecutionErrorClassification(
                declared_type, declared_type
            )
        return None

    def _recover_planned_execution(
        self,
        task: DiscoveryTaskInput,
        plan: CrawlPlan,
        trajectory: TrajectoryBuffer,
        snapshot_context: dict | None,
        failed_result: DiscoveryRunResult | None = None,
    ) -> DiscoveryRunResult | None:
        """Apply the fixed PATH B/C recovery table without returning final jobs."""
        classification = self._classify_execution_context(snapshot_context)
        if classification is None:
            return None
        next_action = error_classifier.classify_next_action(classification.error_type)
        if next_action == "needs_manual_review":
            if failed_result is not None:
                failed_result.status = "needs_manual_review"
                failed_result.block_reason = classification.reason
                failed_result.summary = f"Crawl requires manual review: {classification.reason}"
                return failed_result
            return DiscoveryRunResult(
                status="needs_manual_review",
                block_reason=classification.reason,
                summary=f"Crawl requires manual review: {classification.reason}",
            )
        if next_action == "partial_success":
            if failed_result is not None:
                failed_result.status = "partial_success"
                failed_result.block_reason = classification.reason
                failed_result.summary = f"Crawl returned partial data: {classification.reason}"
                return failed_result
            return DiscoveryRunResult(
                status="partial_success",
                block_reason=classification.reason,
                summary=f"Crawl returned partial data: {classification.reason}",
            )
        if next_action == "resume_path_b":
            return self._execute_planned_crawl(task, plan, trajectory, snapshot_context)

        try:
            planner = build_crawl_plan_agent(
                settings=self.settings,
                snapshot_context=snapshot_context,
            )
            repaired_plan = repair_crawl_plan(
                task,
                plan,
                snapshot_context or {},
                planner,
                max_inspection_pages=self.settings.job_discovery_planner_max_inspection_pages,
                settings=self.settings,
            )
        except Exception as exc:
            return self._planner_failure_result(exc)
        return self._execute_planned_crawl(
            task, repaired_plan, trajectory, snapshot_context
        )

    def run_once(self) -> int:
        """Claim and process one discovery task.

        Returns
        -------
        int
            ``1`` if a task was claimed and processed, ``0`` if the queue
            was empty.
        """
        db = self.db_factory()
        task: JobDiscoveryTask | None = None
        try:
            # 1. Claim a task from the queue
            task = claim_next_task(
                db,
                worker_id=self.worker_id,
                lease_seconds=self.settings.job_discovery_task_timeout_seconds,
            )
            if task is None:
                return 0

            # 2. Load the raw record for record_fields
            raw_record = db.scalar(
                select(RawJobRecord).where(RawJobRecord.id == task.raw_record_id)
            )
            record_fields: list[dict[str, Any]] = (
                raw_record.raw_fields if raw_record else []
            )

            # 3. Build task input
            task_input = DiscoveryTaskInput(
                source_id=task.source_id,
                raw_record_id=task.raw_record_id,
                external_record_id=task.external_record_id,
                source_key=task.source_key,
                source_url=task.source_url,
                url_hash=task.url_hash,
                record_fields=record_fields,
            )

            # ── 4a. Strategy routing ──────────────────────────────────
            strategy_record: StrategyRecord | None = None
            trajectory: TrajectoryBuffer | None = None
            snapshot_context: dict | None = None
            executor_type: str = "supervisor"

            # Build the navigation dependencies only when a legacy snapshot
            # declares that tool.  CrawlPlan execution is deterministic and
            # must not require an LLM credential.
            _llm: Any = None
            _tool_dependencies: dict[str, Any] | None = None
            if self.settings.job_discovery_strategy_enabled:
                router = StrategyRouter(db)
                matched = router.match(task.source_url)
                if matched is not None:
                    strategy_record = StrategyRecord.from_orm(matched)
                    trajectory = TrajectoryBuffer(
                        task_id=task.id,
                        strategy_id=strategy_record.id,
                        executor_type=(
                            "adapter" if strategy_record.adapter else "snapshot"
                        ),
                    )

                    if strategy_record.adapter:
                        # ── Fast lane: DomainAdapter ──
                        executor_type = "adapter"
                        try:
                            adapter_instance = _load_adapter(strategy_record.adapter)
                            if (
                                isinstance(adapter_instance, CompleteCrawlAdapter)
                                and not self.settings.job_discovery_pev_enabled
                            ):
                                snapshot_context = {
                                    "source": "crawl_plan",
                                    "block_reason": "pev_disabled",
                                }
                                executor_type = "supervisor"
                                strategy_record = None
                            else:
                                result = adapter_instance.execute(
                                    task_input, strategy_record, trajectory
                                )
                        except Exception as adapter_exc:
                            trajectory.record_step(
                                strategy_record.adapter, "failed",
                                {"url": task.source_url}, None,
                                error=adapter_exc,
                            )
                            snapshot_context = trajectory.to_snapshot_context()
                            executor_type = "supervisor"
                            # Increment error count BEFORE clearing strategy_record
                            strat_store.increment_error_count(db, strategy_record.id, {
                                "tool": strategy_record.adapter,
                                "reason": error_classifier.classify_error(str(adapter_exc)),
                                "message": str(adapter_exc)[:500],
                            })
                            strategy_record = None  # Clear so supervisor outcome isn't double-counted
                    else:
                        # ── Fast path: SnapshotExecutor ──
                        crawl_plan_declared = _declares_crawl_plan(
                            strategy_record.plan_yaml
                        )

                        if (
                            crawl_plan_declared
                            and not self.settings.job_discovery_pev_enabled
                        ):
                            snapshot_context = {
                                "source": "crawl_plan",
                                "block_reason": "pev_disabled",
                            }
                            executor_type = "supervisor"
                            strategy_record = None
                        else:
                            executor_type = "crawl_plan" if crawl_plan_declared else "snapshot"
                            if not crawl_plan_declared and _snapshot_uses_runtime_navigation(
                                strategy_record.plan_yaml
                            ):
                                _llm = _build_job_discovery_llm(self.settings)
                                _web_nav_subagent = create_web_navigation_subagent(self.settings)
                                _tool_dependencies = {
                                    "run_web_navigation": _make_run_web_navigation_wrapper(
                                        self.settings, _web_nav_subagent, _llm
                                    ),
                                }
                            crawl_driver_factory = (
                                self._crawl_driver_factory
                                or (lambda _plan, _task: _build_playwright_crawl_driver(self.settings))
                                if crawl_plan_declared
                                else None
                            )
                            # Hard deadline for SnapshotPlans that run real
                            # network fetches (WeChat fetch_wechat_article).
                            # When enabled (>0), fetch_wechat_article runs in a
                            # spawned subprocess killed at the deadline -> a
                            # needs_manual_review / task_deadline_exceeded
                            # result instead of an unbounded hang. Disabled
                            # (0) by default; opt-in via env for the gray roll.
                            _snapshot_deadline = (
                                self.settings.job_discovery_snapshot_deadline_seconds
                                or None
                            )
                            snap = SnapshotExecutor(
                                strategy_record,
                                task_input,
                                trajectory,
                                tool_dependencies=_tool_dependencies,
                                crawl_driver_factory=crawl_driver_factory,
                                deadline_seconds=_snapshot_deadline,
                                hard_timeout_tools=(
                                    {"fetch_wechat_article"}
                                    if _snapshot_deadline
                                    else None
                                ),
                            )
                            snap_result = snap.execute()
                            if isinstance(snap_result, SnapshotExecutionResult) and snap_result.needs_supervisor_fallback:
                                recovered: DiscoveryRunResult | None = None
                                if crawl_plan_declared and _planner_enabled(self.settings):
                                    try:
                                        failed_plan = CrawlPlan.from_yaml(strategy_record.plan_yaml)
                                        failure_context = self._execution_failure_context(
                                            snap_result, snap_result.snapshot_context
                                        )
                                        recovered = self._recover_planned_execution(
                                            task_input,
                                            failed_plan,
                                            trajectory,
                                            failure_context,
                                        )
                                    except (TypeError, ValueError):
                                        recovered = None
                                if recovered is not None:
                                    result = recovered
                                    executor_type = "crawl_plan"
                                elif (
                                    crawl_plan_declared
                                    and _planner_enabled(self.settings)
                                    and not self.settings.job_discovery_legacy_path_c_enabled
                                ):
                                    result = DiscoveryRunResult(
                                        status="needs_manual_review",
                                        block_reason="planner_unavailable",
                                        summary="Planner repair failed and legacy PATH C is disabled",
                                    )
                                    executor_type = "crawl_plan"
                                else:
                                    snapshot_context = snap_result.snapshot_context
                                    executor_type = "supervisor"
                                    # Increment error count BEFORE clearing strategy_record
                                    fail_idx = trajectory.failed_step_index
                                    if fail_idx is not None and trajectory.steps:
                                        failed_step = trajectory.steps[fail_idx]
                                        strat_store.increment_error_count(db, strategy_record.id, {
                                            "tool": failed_step.get("tool", "snapshot"),
                                            "reason": error_classifier.classify_error(failed_step.get("error", "")),
                                            "message": str(failed_step.get("error", ""))[:500],
                                        })
                                    strategy_record = None
                            else:
                                result = snap_result
                                if (
                                    crawl_plan_declared
                                    and _planner_enabled(self.settings)
                                    and result.coverage is not None
                                ):
                                    failure_context = self._execution_failure_context(
                                        result, None
                                    )
                                    if failure_context is not None:
                                        try:
                                            active_plan = CrawlPlan.from_yaml(
                                                strategy_record.plan_yaml
                                            )
                                            recovered = self._recover_planned_execution(
                                                task_input,
                                                active_plan,
                                                trajectory,
                                                failure_context,
                                                failed_result=result,
                                            )
                                        except (TypeError, ValueError):
                                            recovered = None
                                        if recovered is not None:
                                            result = recovered

            # ── 4aa. PATH C planning for an unknown / unstrategized site ──
            # PEV remains fully opt-in: without both flags the legacy
            # Supervisor path below is untouched.
            if (
                executor_type == "supervisor"
                and trajectory is None
                and _planner_enabled(self.settings)
            ):
                trajectory = TrajectoryBuffer(
                    task_id=task.id,
                    strategy_id=None,
                    executor_type="planner",
                )
                try:
                    planner = build_crawl_plan_agent(settings=self.settings, model=_llm)
                    generated_plan = generate_crawl_plan(
                        task_input,
                        planner,
                        max_inspection_pages=self.settings.job_discovery_planner_max_inspection_pages,
                        settings=self.settings,
                    )
                    result = self._execute_planned_crawl(
                        task_input, generated_plan, trajectory
                    )
                    executor_type = "crawl_plan"
                    failure_context = self._execution_failure_context(result, None)
                    if result.coverage is not None and failure_context is not None:
                        recovered = self._recover_planned_execution(
                            task_input,
                            generated_plan,
                            trajectory,
                            failure_context,
                            failed_result=result,
                        )
                        if recovered is not None:
                            result = recovered
                    elif (
                        isinstance(result, SnapshotExecutionResult)
                        and result.needs_supervisor_fallback
                    ):
                        failure_context = self._execution_failure_context(
                            result, result.snapshot_context
                        )
                        recovered = self._recover_planned_execution(
                            task_input,
                            generated_plan,
                            trajectory,
                            failure_context,
                        )
                        if recovered is not None:
                            result = recovered
                        elif self.settings.job_discovery_legacy_path_c_enabled:
                            snapshot_context = result.snapshot_context
                            executor_type = "supervisor"
                        else:
                            result = DiscoveryRunResult(
                                status="needs_manual_review",
                                block_reason="planner_unavailable",
                                summary="Planner repair failed and legacy PATH C is disabled",
                            )
                except Exception as exc:
                    fallback_result = self._planner_failure_result(exc)
                    if fallback_result is not None:
                        result = fallback_result
                        executor_type = "planner"
                    elif self.settings.job_discovery_legacy_path_c_enabled:
                        snapshot_context = {"source": "planner", "error": str(exc)}
                    else:
                        result = DiscoveryRunResult(
                            status="needs_manual_review",
                            block_reason="planner_unavailable",
                            summary="Planner failed and legacy PATH C is disabled",
                        )
                        executor_type = "planner"

            # ── 4b. Supervisor Agent (backup path or primary if no match) ──
            agent_error: Exception | None = None
            if executor_type == "supervisor":
                if trajectory is None:
                    trajectory = TrajectoryBuffer(
                        task_id=task.id,
                        strategy_id=None,
                        executor_type="supervisor",
                    )
                try:
                    agent = build_discovery_supervisor_agent(
                        settings=self.settings,
                        model=_llm,
                        snapshot_context=snapshot_context,
                    )
                    agent_input = {
                        "messages": [
                            HumanMessage(
                                content=json.dumps(asdict(task_input), ensure_ascii=False)
                            )
                        ]
                    }
                    try:
                        if snapshot_context is not None:
                            result_raw = agent.invoke(agent_input, config={"recursion_limit": 30})
                        else:
                            result_raw = agent.invoke(agent_input, config={"recursion_limit": 50})
                    except TypeError as exc:
                        if "config" not in str(exc):
                            raise
                        result_raw = agent.invoke(agent_input)
                    try:
                        result = parse_agent_result(result_raw)
                    except AgentResultParseError as parse_error:
                        trajectory.record_step(
                            "supervisor_parse_failed",
                            "failed",
                            {
                                "message_types": parse_error.message_types,
                                "message_count": parse_error.message_count,
                            },
                            {"stop_reason": "parse_failed"},
                        )
                        result = DiscoveryRunResult(
                            status="failed",
                            block_reason="parse_failed",
                            summary=str(parse_error),
                        )
                    else:
                        trajectory.record_step(
                            "supervisor_complete", "ok", {}, {"status": result.status}
                        )
                except Exception as agent_exc:
                    agent_error = agent_exc
                    trajectory.record_step("supervisor_fatal", "failed", {}, None, error=agent_exc)
                    result = DiscoveryRunResult(
                        status="failed",
                        summary=f"Agent invocation failed: {agent_exc}",
                    )

            # ── 4c. Fallback recovery ──────────────────────────────────
            result = _fallback_with_record_fields_if_agent_missed_evidence(
                result,
                task=task,
                task_input=task_input,
                settings=self.settings,
            )
            if result.coverage is None:
                result = enforce_result_invariants(result)
                coverage_decision = None
            else:
                coverage_decision = verify_coverage(result.coverage)
                failure_context = self._execution_failure_context(result, None)
                classification = self._classify_execution_context(failure_context)
                next_action = (
                    error_classifier.classify_next_action(classification.error_type)
                    if classification is not None
                    else None
                )
                if next_action == "needs_manual_review":
                    result.status = "needs_manual_review"
                    result.block_reason = classification.reason
                elif next_action == "partial_success":
                    result.status = "partial_success"
                    result.block_reason = classification.reason
                else:
                    result.status = coverage_decision.status
                    result.block_reason = (
                        None if coverage_decision.complete else coverage_decision.reason
                    )
            if agent_error is not None and not result.candidates and not result.evidence:
                raise agent_error

            # 6. Persist evidence and candidates
            _persist_evidence(db, task, result.evidence)
            _persist_candidates(db, task, result.candidates)

            # 7. Mark task according to status
            summary_json: dict[str, Any] = {
                "summary": result.summary,
                "evidence_count": len(result.evidence),
                "candidate_count": len(result.candidates),
                "execution_path": (
                    "crawl_plan" if result.coverage is not None else executor_type
                ),
                "coverage_verified": (
                    coverage_decision.complete if coverage_decision is not None else False
                ),
                "coverage": asdict(result.coverage) if result.coverage is not None else None,
            }

            if result.status in ("succeeded", "partial_success"):
                if result.status == "succeeded":
                    mark_task_succeeded(db, task, result_summary_json=summary_json)
                else:
                    mark_task_partial_success(
                        db, task, result_summary_json=summary_json
                    )
            elif result.status == "needs_manual_review":
                block_reason = DiscoveryBlockReason.unknown
                if result.block_reason is not None:
                    try:
                        block_reason = DiscoveryBlockReason(result.block_reason)
                    except ValueError:
                        pass
                mark_task_needs_manual_review(
                    db,
                    task,
                    block_reason=block_reason,
                    result_summary_json=summary_json,
                )
            else:
                mark_task_failed(
                    db, task, last_error=result.summary or "Agent returned failed status"
                )

            # ── 7b. Save trajectory ────────────────────────────────────
            try:
                url_pattern = _extract_url_pattern(task.source_url)
                trajectory_id = save_trajectory(
                    db, trajectory, result,
                    url=task.source_url,
                    url_pattern=url_pattern,
                )
                # Schedule annotation for supervisor-only and fallback paths
                if executor_type == "supervisor" or result.status == "partial_fallback":
                    if self.settings.trajectory_annotation_enabled:
                        schedule_annotation(db, trajectory_id)
            except Exception:
                pass  # trajectory save failure should not fail the task

            # ── 7c. Update strategy counters ──────────────────────────
            if strategy_record is not None:
                try:
                    strategy_id = strategy_record.id
                    if trajectory.failed_step_index is not None:
                        strat_store.increment_error_count(
                            db, strategy_id,
                            last_error={
                                "tool": (trajectory.steps[trajectory.failed_step_index].get("tool")
                                         if trajectory.steps else "unknown"),
                                "reason": error_classifier.classify_error(
                                    trajectory.steps[trajectory.failed_step_index].get("error", "")
                                    if trajectory.steps else ""
                                ),
                                "message": (trajectory.steps[trajectory.failed_step_index].get("error", "")
                                            if trajectory.steps else ""),
                            },
                        )
                    else:
                        strat_store.increment_success(
                            db, strategy_id,
                            duration_s=trajectory.elapsed_ms / 1000.0,
                        )
                except Exception:
                    pass  # strategy counter updates are best-effort

            db.commit()
            return 1

        except Exception as exc:
            if task is not None:
                try:
                    mark_task_failed(db, task, last_error=str(exc))
                    db.commit()
                except Exception:
                    db.rollback()
            return 0
        finally:
            db.close()

    def run_loop(self, *, poll_interval: float = 10.0) -> None:
        """Continuously poll and process tasks until interrupted.

        Parameters
        ----------
        poll_interval:
            Seconds to sleep between polls when the queue is empty.
        """
        try:
            while True:
                processed = self.run_once()
                if processed == 0:
                    self._idle_cycles += 1
                    if self._idle_cycles % 10 == 0:
                        self._run_health_checks()
                    time.sleep(poll_interval)
                else:
                    self._idle_cycles = 0
        except KeyboardInterrupt:
            pass

    def _run_health_checks(self) -> None:
        """Run periodic health checks on strategies due for checking.

        Queries strategies that haven't been checked within the configured
        interval, performs an HTTP HEAD against each derived base URL, and
        records the result.  Failures are caught and logged via the strategy
        store's error counter so the strategy can be automatically degraded.

        Also purges old trajectories based on ``trajectory_retention_days``.
        """
        try:
            db = self.db_factory()
            try:
                # ── Purge old trajectories ──────────────────────────────
                if self.settings.trajectory_retention_days > 0:
                    try:
                        deleted = purge_old_trajectories(
                            db, self.settings.trajectory_retention_days
                        )
                        if deleted > 0:
                            logger.info("Purged %d old trajectories", deleted)
                    except Exception:
                        db.rollback()

                # ── Health checks ───────────────────────────────────────
                due = strat_store.get_strategies_due_for_health_check(
                    db,
                    interval_hours=self.settings.strategy_health_check_interval_hours,
                )
                import httpx
                with httpx.Client(timeout=10.0) as client:
                    for strategy in due:
                        check_url = _derive_health_check_url(strategy.url_pattern)
                        if not check_url:
                            continue
                        try:
                            resp = client.head(check_url, follow_redirects=True)
                            strat_store.record_health_check(
                                db, strategy.id,
                                ok=resp.is_success,
                                detail=f"HTTP {resp.status_code}",
                            )
                        except Exception as hc_exc:
                            strat_store.record_health_check(
                                db, strategy.id,
                                ok=False,
                                detail=str(hc_exc),
                            )
                db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()
        except Exception:
            pass  # never crash the worker over a health check
