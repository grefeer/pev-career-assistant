"""SnapshotExecutor -- deterministic replay of a strategy's YAML plan steps.

Calls the same tool functions as the Supervisor Agent but without LLM planning.
On step failure, returns a SnapshotExecutionResult that signals the caller to
hand over to the Supervisor Agent with snapshot_context injected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

import yaml

from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    DiscoveryTaskInput,
    NormalizedJobCandidate,
    PageEvidence,
    StrategyRecord,
)
from backend.app.services.job_discovery.crawling.checkpoint import CrawlCheckpoint
from backend.app.services.job_discovery.crawling.crawl_executor import CrawlExecutor
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.driver import CrawlDriver
from backend.app.services.job_discovery.post_crawl_pipeline import run_post_crawl_pipeline
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


@dataclass
class SnapshotExecutionResult(DiscoveryRunResult):
    """Extended result carrying snapshot_context when Supervisor takeover is needed."""

    needs_supervisor_fallback: bool = False
    snapshot_context: dict[str, Any] | None = None


class CrawlPlanStructureError(RuntimeError):
    """A plan cannot be safely executed without Planner repair."""


class SnapshotExecutor:
    """Replay a YAML plan step-by-step against real tool functions.

    On any step failure, short-circuits and returns a result with embedded
    snapshot_context for Supervisor takeover.
    """

    # Field aliases for domain objects where logical template names differ
    # from actual attribute names (e.g. {{task.url}} -> task.source_url)
    _FIELD_ALIASES: dict[str, dict[str, str]] = {
        "DiscoveryTaskInput": {"url": "source_url"},
    }

    def __init__(
        self,
        strategy: StrategyRecord,
        task: DiscoveryTaskInput,
        trajectory: TrajectoryBuffer,
        tool_dependencies: dict[str, Any] | None = None,
        crawl_driver_factory: Callable[[CrawlPlan, DiscoveryTaskInput], CrawlDriver]
        | None = None,
        checkpoint: CrawlCheckpoint | None = None,
    ) -> None:
        self.strategy = strategy
        self.task = task
        self.trajectory = trajectory
        self._context: dict[str, Any] = {"task": task, "prev": None}
        self._runtime_tools: dict[str, Any] = {}
        self._crawl_driver_factory = crawl_driver_factory
        self._checkpoint = checkpoint
        if tool_dependencies:
            self._inject_runtime_tools(tool_dependencies)

    def _inject_runtime_tools(self, deps: dict[str, Any]) -> None:
        """Inject runtime-dependent tools (settings/model/subagent) from worker context.

        Currently wraps ``run_web_navigation`` which depends on objects
        (settings, model, subagent) only available inside the Supervisor.
        """
        run_web_navigation = deps.get("run_web_navigation")
        if run_web_navigation:
            self._runtime_tools["run_web_navigation"] = run_web_navigation

    def execute(self) -> DiscoveryRunResult:
        """Execute the plan. Returns SnapshotExecutionResult on any step failure."""
        if self._declares_crawl_plan():
            try:
                return self._execute_crawl_plan(self._crawl_plan())
            except (KeyError, TypeError, ValueError) as exc:
                return self._crawl_structure_failure(
                    exc,
                    self._checkpoint_for_declared_crawl_plan(),
                )
        steps = self._parse_plan()
        completed: list[dict[str, Any]] = []

        for i, step in enumerate(steps):
            params = self._resolve_template(step.get("params", {}))
            tool_name = step["tool"]

            # All step errors are terminal -- trigger Supervisor fallback
            try:
                result = _call_tool_by_name(tool_name, executor=self, **params)
                self.trajectory.record_step(tool_name, "ok", params, result)
                self._context["prev"] = {"result": result}
                completed.append({"tool": tool_name, "params": params, "result": result})
            except Exception as exc:
                self.trajectory.record_step(tool_name, "failed", params, None, error=exc)
                return SnapshotExecutionResult(
                    status="failed",
                    summary=f"Snapshot step {i+1} ({tool_name}) failed: {exc}",
                    needs_supervisor_fallback=True,
                    snapshot_context=self.trajectory.to_snapshot_context(),
                )

        # All steps succeeded -- construct final result from collected outputs
        return self._build_final_result(completed)

    # -- internal -----------------------------------------------------------

    def _parse_plan(self) -> list[dict[str, Any]]:
        """Parse the YAML plan_yaml string into step dicts."""
        parsed = yaml.safe_load(self.strategy.plan_yaml)
        if isinstance(parsed, dict) and "plan" in parsed:
            return parsed["plan"]
        if isinstance(parsed, list):
            return parsed
        raise ValueError(f"Invalid plan_yaml format for strategy {self.strategy.id}")

    def _declares_crawl_plan(self) -> bool:
        try:
            parsed = yaml.safe_load(self.strategy.plan_yaml)
        except yaml.YAMLError:
            return False
        return isinstance(parsed, dict) and parsed.get("plan_type") == "crawl_plan"

    def _crawl_plan(self) -> CrawlPlan:
        return CrawlPlan.from_yaml(self.strategy.plan_yaml)

    def _checkpoint_for_declared_crawl_plan(self) -> CrawlCheckpoint:
        try:
            parsed = yaml.safe_load(self.strategy.plan_yaml)
        except yaml.YAMLError:
            parsed = {}
        version = parsed.get("version", 1) if isinstance(parsed, dict) else 1
        try:
            plan_version = int(version)
        except (TypeError, ValueError):
            plan_version = 1
        return CrawlCheckpoint(plan_version=plan_version, source_url=self.task.source_url)

    def _execute_crawl_plan(self, plan: CrawlPlan) -> DiscoveryRunResult:
        checkpoint = self._checkpoint or CrawlCheckpoint(
            plan_version=plan.version,
            source_url=self.task.source_url,
        )
        driver: CrawlDriver | None = None
        try:
            if self._crawl_driver_factory is None:
                raise CrawlPlanStructureError("crawl driver factory is unavailable")
            driver = self._crawl_driver_factory(plan, self.task)
            crawl_result = CrawlExecutor(driver, self.trajectory).execute(
                plan=plan,
                task=self.task,
                checkpoint=checkpoint,
            )
            if crawl_result.error and _is_structural_crawl_error(crawl_result.error):
                raise CrawlPlanStructureError(crawl_result.error)
            return run_post_crawl_pipeline(self.task, crawl_result)
        except Exception as exc:
            return self._crawl_structure_failure(exc, checkpoint)
        finally:
            close = getattr(driver, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    self.trajectory.record_step("crawl_cleanup", "failed", {})

    def _crawl_structure_failure(
        self,
        exc: Exception,
        checkpoint: CrawlCheckpoint,
    ) -> SnapshotExecutionResult:
        self.trajectory.record_step(
            "crawl_plan",
            "failed",
            {"plan_version": checkpoint.plan_version},
            error=exc,
        )
        context = self.trajectory.to_snapshot_context()
        failed_step = context.get("failed_step")
        if isinstance(failed_step, dict):
            failed_step["error_type"] = "structure_error"
        context["checkpoint"] = checkpoint.to_dict()
        return SnapshotExecutionResult(
            status="failed",
            summary=f"Crawl plan structure failed: {type(exc).__name__}",
            needs_supervisor_fallback=True,
            snapshot_context=context,
        )

    def _resolve_template(
        self,
        params: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve ``{{}}`` template variables in param values.

        When *context* is provided it is used instead of the executor's
        internal ``self._context`` (useful for testing isolated resolution).
        Missing fields resolve to Python ``None``.
        """
        ctx = context if context is not None else self._context
        resolved: dict[str, Any] = {}
        for key, value in params.items():
            if isinstance(value, str) and "{{" in value:
                resolved[key] = self._substitute(value, ctx)
            else:
                resolved[key] = value
        return resolved

    def _substitute(self, template: str, context: dict[str, Any]) -> Any:
        """Substitute a single ``{{...}}`` expression against *context*.

        Supports::

            {{task.url}}, {{task.source_key}}, {{prev.result}},
            {{prev.result.xxx}}

        Single-level nesting only (``task`` / ``prev`` are the root keys).
        Missing fields resolve to Python ``None``.

        The ``"None"`` string ambiguity is handled by the caller
        (``_resolve_template``) which only invokes this method when
        ``"{{"`` is present in the value -- a literal string ``"None"``
        is therefore never passed here.
        """
        _aliases = self._FIELD_ALIASES

        def _replacer(match: re.Match) -> str:
            expr = match.group(1).strip()
            parts = expr.split(".")
            value: Any = context
            for i, p in enumerate(parts):
                if isinstance(value, dict):
                    value = value.get(p)
                elif isinstance(value, (list, tuple)) and isinstance(p, int):
                    try:
                        value = value[p]
                    except (IndexError, TypeError):
                        value = None
                elif hasattr(value, p):
                    value = getattr(value, p, None)
                else:
                    # Check field aliases for known domain types
                    cls_name = type(value).__name__
                    if cls_name in _aliases and p in _aliases[cls_name]:
                        aliased = _aliases[cls_name][p]
                        if hasattr(value, aliased):
                            value = getattr(value, aliased, None)
                        else:
                            value = None
                    else:
                        value = None
                    if value is None:
                        break
            if value is None:
                return "None"
            if isinstance(value, (dict, list)):
                import json

                return json.dumps(value, ensure_ascii=False)
            return str(value)

        result = re.sub(r"\{\{(.+?)\}\}", _replacer, template)
        if result == "None":
            return None
        return result

    def _build_final_result(self, completed: list[dict[str, Any]]) -> DiscoveryRunResult:
        """Build ``DiscoveryRunResult`` from the completed steps."""
        evidence: list[PageEvidence] = []
        candidates: list[NormalizedJobCandidate] = []

        for step in completed:
            result = step.get("result")
            if isinstance(result, dict):
                if "evidence_type" in result:
                    evidence.append(PageEvidence(**result))
                elif isinstance(result.get("candidates"), list):
                    candidates_data = result["candidates"]
                    candidates.extend(
                        NormalizedJobCandidate(**c)
                        for c in candidates_data
                        if isinstance(c, dict)
                    )
                elif isinstance(result.get("evidence"), list):
                    evidence_data = result["evidence"]
                    evidence.extend(
                        PageEvidence(**e)
                        for e in evidence_data
                        if isinstance(e, dict)
                    )
            if isinstance(result, list) and result:
                first = result[0] if isinstance(result[0], dict) else None
                if first and isinstance(first, dict):
                    if "evidence_type" in first:
                        evidence.extend(
                            PageEvidence(**e)
                            for e in result
                            if isinstance(e, dict)
                        )
                    else:
                        candidates.extend(
                            NormalizedJobCandidate(**c)
                            for c in result
                            if isinstance(c, dict)
                        )

        # If the last step returned a JSON string, try to parse
        last_result = completed[-1].get("result") if completed else None
        if isinstance(last_result, str):
            import json

            try:
                parsed = json.loads(last_result)
                if isinstance(parsed, list):
                    candidates.extend(
                        NormalizedJobCandidate(**c)
                        for c in parsed
                        if isinstance(c, dict)
                    )
            except (json.JSONDecodeError, TypeError):
                pass

        # Auto-generate evidence from text-producing intermediate steps
        # when no explicit evidence was found (e.g. WeChat fetch -> extract).
        if candidates and not evidence:
            import hashlib as _hashlib
            for step in completed:
                result = step.get("result")
                if isinstance(result, dict) and result.get("text") and not result.get("evidence_type"):
                    text = result.get("text", "")
                    src_url = result.get("url", "")
                    src_title = result.get("title", "")
                    evidence.append(PageEvidence(
                        evidence_type="rendered_page",
                        url=src_url,
                        title=src_title or "",
                        content_hash=_hashlib.sha256(text.encode()).hexdigest(),
                        text_excerpt=text[:5000],
                        metadata={
                            "source": "snapshot_auto",
                            "tool": step.get("tool", ""),
                        },
                    ))
                    break  # one evidence entry suffices

        return DiscoveryRunResult(
            status="succeeded",
            evidence=evidence,
            candidates=candidates,
            summary=(
                f"SnapshotExecutor completed {len(completed)} steps, "
                f"found {len(candidates)} candidate(s)"
            ),
        )


# ---------------------------------------------------------------------------
# Crawl-plan structural failure classification
# ---------------------------------------------------------------------------


def _is_structural_crawl_error(error: str) -> bool:
    """Classify deterministic declaration failures that require plan repair."""
    error_type = error.partition(":")[0]
    return error_type in {
        "SelectorNotFoundError",
        "ApiPayloadChangedError",
        "UnsafePlanExecutionError",
        "UnsupportedPaginationError",
    }


# ---------------------------------------------------------------------------
# Tool dispatch -- maps tool name strings from YAML to actual functions
# ---------------------------------------------------------------------------

_TOOL_REGISTRY: dict[str, Any] = {}


def _ensure_tool_registry() -> None:
    """Lazy-init the tool registry to avoid circular imports."""
    if _TOOL_REGISTRY:
        return
    from backend.app.services.job_discovery.deepagents_runner import (
        extract_jd_candidates,
        fetch_wechat_article,
        finish_with_manual_review,
        package_candidates,
        parse_wechat_article,
        run_ocr,
        standardize_from_record_fields,
        triage_link,
        verify_evidence,
    )

    _TOOL_REGISTRY.update({
        "triage_link": triage_link,
        "fetch_wechat_article": fetch_wechat_article,
        "parse_wechat_article": parse_wechat_article,
        "run_ocr": run_ocr,
        "extract_jd_candidates": extract_jd_candidates,
        "verify_evidence": verify_evidence,
        "package_candidates": package_candidates,
        "standardize_from_record_fields": standardize_from_record_fields,
        "finish_with_manual_review": finish_with_manual_review,
        "run_web_navigation": None,  # runtime-injected via worker context, not in static registry
    })


def _call_tool_by_name(name: str, *, executor: Any = None, **kwargs: Any) -> Any:
    """Call a tool function by its YAML name.

    Checks runtime-injected tools (per-instance, set by worker via
    ``tool_dependencies``) first, then falls back to the static
    module-level registry.
    """
    # Check runtime tools first (per-instance, injected by worker)
    if executor is not None and name in executor._runtime_tools:
        return executor._runtime_tools[name](**kwargs)
    # Fall back to static registry
    _ensure_tool_registry()
    tool = _TOOL_REGISTRY.get(name)
    if tool is None:
        raise ValueError(f"Unknown or unavailable tool in snapshot: {name}")
    return tool(**kwargs)
