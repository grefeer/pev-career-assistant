"""SnapshotExecutor -- deterministic replay of a strategy's YAML plan steps.

Calls the same tool functions as the Supervisor Agent but without LLM planning.
On step failure, returns a SnapshotExecutionResult that signals the caller to
hand over to the Supervisor Agent with snapshot_context injected.
"""
from __future__ import annotations

import re
import time
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


class _HardDeadlineExceeded(RuntimeError):
    """Internal signal: a hard-timeout tool was terminated by the deadline.

    Caught by :meth:`SnapshotExecutor.execute` and converted into a
    ``needs_manual_review`` / ``task_deadline_exceeded`` result -- never
    escalated to the Supervisor/WebNavigationAgent.
    """


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
        deadline_seconds: float | None = None,
        hard_timeout_tools: set[str] | None = None,
    ) -> None:
        self.strategy = strategy
        self.task = task
        self.trajectory = trajectory
        self._context: dict[str, Any] = {"task": task, "prev": None}
        self._runtime_tools: dict[str, Any] = {}
        self._crawl_driver_factory = crawl_driver_factory
        self._checkpoint = checkpoint
        # Single absolute deadline (Task 6): once ``_deadline_at`` passes, every
        # remaining step short-circuits to ``task_deadline_exceeded``. Tools in
        # ``hard_timeout_tools`` (WeChat ``fetch_wechat_article``) additionally
        # run in a spawned subprocess so a blocked socket is actually killed
        # instead of leaking a thread.
        self._deadline_at: float | None = (
            time.monotonic() + deadline_seconds if deadline_seconds else None
        )
        self._hard_timeout_tools: set[str] = set(hard_timeout_tools or ())
        if tool_dependencies:
            self._inject_runtime_tools(tool_dependencies)

    def _inject_runtime_tools(self, deps: dict[str, Any]) -> None:
        """Inject runtime-dependent tools from worker context.

        Tools that depend on objects only available inside the Supervisor
        (``run_web_navigation`` needs settings/model/subagent) -- or that are
        replaced by a hang fixture in deadline tests (``fetch_wechat_article``)
        -- are supplied by the caller and take precedence over the static
        registry on dispatch.
        """
        for name, tool in deps.items():
            if tool is not None:
                self._runtime_tools[name] = tool

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

            # ── Hard deadline: stop before this step if already exhausted ──
            remaining = self._remaining_seconds()
            if remaining is not None and remaining <= 0:
                return self._deadline_exceeded(
                    tool_name, i, params, "deadline exhausted before step"
                )

            # All step errors are terminal -- trigger Supervisor fallback.
            # ``_HardDeadlineExceeded`` (a hard-timeout tool was killed) is
            # caught first and converted to a manual-review deadline result
            # rather than a Supervisor takeover.
            try:
                if (
                    tool_name in self._hard_timeout_tools
                    and self._deadline_at is not None
                ):
                    result = self._call_with_hard_timeout(
                        tool_name, params, remaining
                    )
                else:
                    result = _call_tool_by_name(tool_name, executor=self, **params)
                self.trajectory.record_step(tool_name, "ok", params, result)
                self._context["prev"] = {"result": result}
                completed.append({"tool": tool_name, "params": params, "result": result})
            except _HardDeadlineExceeded:
                return self._deadline_exceeded(
                    tool_name, i, params, "subprocess deadline exceeded"
                )
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

    def _remaining_seconds(self) -> float | None:
        """Wall-clock seconds left until the hard deadline, or ``None`` if unset."""
        if self._deadline_at is None:
            return None
        return self._deadline_at - time.monotonic()

    def _deadline_exceeded(
        self,
        tool_name: str,
        step_index: int,
        params: dict[str, Any],
        reason: str,
    ) -> SnapshotExecutionResult:
        """Build the ``task_deadline_exceeded`` manual-review result.

        A deadline is a per-task timeout, not a strategy/structure failure:
        the result must NOT trigger Supervisor/WebNavigationAgent takeover
        (``needs_supervisor_fallback=False``).
        """
        self.trajectory.record_step(
            tool_name,
            "deadline_exceeded",
            params,
            None,
            error=RuntimeError(reason),
        )
        return SnapshotExecutionResult(
            status="needs_manual_review",
            block_reason="task_deadline_exceeded",
            summary=f"Snapshot step {step_index+1} ({tool_name}) hit the hard deadline: {reason}",
            needs_supervisor_fallback=False,
            snapshot_context=self.trajectory.to_snapshot_context(),
        )

    def _call_with_hard_timeout(
        self,
        tool_name: str,
        params: dict[str, Any],
        remaining: float | None,
    ) -> Any:
        """Run a hard-timeout tool in a spawned subprocess bounded by remaining.

        Forwards ``deadline_remaining_seconds`` so the tool can shrink its own
        network timeouts below the subprocess kill bound (graceful failure vs
        a hard kill). A timeout raises :class:`_HardDeadlineExceeded`, which
        :meth:`execute` converts into a manual-review result.
        """
        from backend.app.services.job_discovery.strategy.deadline import (
            run_with_hard_timeout,
        )

        fn = self._resolve_hard_timeout_tool(tool_name)
        call_kwargs = dict(params)
        if "deadline_remaining_seconds" not in call_kwargs and remaining is not None:
            call_kwargs["deadline_remaining_seconds"] = max(0.0, remaining)
        # Bound the subprocess just below the remaining budget so it fires
        # before the next step's pre-check would. Keep a small floor so an
        # already-tight budget still gets a real subprocess kill.
        timeout = remaining if (remaining is not None and remaining > 0) else 0.05
        result = run_with_hard_timeout(
            fn, timeout_seconds=timeout, kwargs=call_kwargs
        )
        if result.timed_out:
            raise _HardDeadlineExceeded(tool_name)
        if result.error:
            # Sanitized error string from the subprocess; surface as a normal
            # step failure (Supervisor fallback may still apply).
            raise RuntimeError(result.error)
        return result.value

    def _resolve_hard_timeout_tool(self, name: str) -> Callable[..., Any]:
        """Resolve the actual function for a hard-timeout tool.

        Runtime-injected tools (e.g. a hang fixture) take precedence; the
        static registry is the fallback. The function must be picklable for
        the spawn subprocess (module-level or a runtime fixture).
        """
        if name in self._runtime_tools:
            return self._runtime_tools[name]
        _ensure_tool_registry()
        fn = _TOOL_REGISTRY.get(name)
        if fn is None:
            raise ValueError(f"Unknown or unavailable hard-timeout tool: {name}")
        return fn

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
