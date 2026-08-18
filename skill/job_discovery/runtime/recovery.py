"""Deterministic discovery-recovery strategies for the PEV runtime.

Business policy for continuing a source-bound discovery route after a model
stall. The runtime injects a ``RecoveryContext`` (tool invocation,
persistence, budget, events) so this module stays free of db/repository
details; every decision here is a job-discovery business rule.
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol

from backend.app.services.agent_runtime.schemas import ExecutorResult, ToolObservation
from backend.app.services.career_skills.discovery_policy import (
    contains_access_block,
    discovery_search_hints,
    public_source_mirror_seed_urls,
    public_urls_from_search_item,
    requested_role_seed_urls,
    search_result_urls,
    trusted_discovery_seed_urls,
)


class RecoveryContext(Protocol):
    """Runtime-owned capabilities the discovery recovery strategies may use."""

    @property
    def user_id(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def task_goal(self) -> str: ...

    @property
    def step_id(self) -> str: ...

    @property
    def task_context(self) -> dict[str, Any]: ...

    @property
    def metadata(self) -> dict[str, Any]: ...

    def has_registered_tool(self, name: str) -> bool: ...

    def invoke_tool(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ToolObservation: ...

    def persist(
        self, execution: ExecutorResult, *, mark: str | None = None
    ) -> list[dict[str, str]]: ...

    def consume_tool_budget(self) -> bool: ...

    def append_event(self, event_type: str, payload: dict[str, Any]) -> None: ...

    def child(self, metadata: dict[str, Any]) -> "RecoveryContext": ...

    def persisted_job_search_observations(
        self, artifact_refs: list[dict[str, Any]]
    ) -> list[ToolObservation]: ...


def auto_extract_jd_details(
    ctx: RecoveryContext, artifact_refs: list[dict[str, Any]]
) -> tuple[list[ToolObservation], list[dict[str, str]]]:
    """Normalize captured JD pages before a model stall becomes a handoff."""
    if any(ref.get("runtime_auto_extract") == "true" for ref in artifact_refs):
        return [], []
    page_ids = [
        str(ref["artifact_id"])
        for ref in artifact_refs
        if ref.get("artifact_type") == "public_job_page"
        and ref.get("quality") == "jd_complete"
        and isinstance(ref.get("artifact_id"), str)
    ]
    if not page_ids:
        return [], []
    page_ids = list(dict.fromkeys(page_ids))
    observations: list[ToolObservation] = []
    refs: list[dict[str, str]] = []
    for offset in range(0, len(page_ids), 10):
        if not ctx.consume_tool_budget():
            break
        observation = ctx.invoke_tool(
            "extract-observed-job-details-batch",
            {"artifact_ids": page_ids[offset : offset + 10]},
        )
        observations.append(observation)
        if observation.status != "succeeded":
            continue
        auto_execution = ExecutorResult(
            status="succeeded",
            observations=[observation],
            summary="已对已抓取的公开 JD 页面执行确定性结构化提取。",
        )
        batch_refs = ctx.persist(auto_execution, mark="runtime_auto_extract")
        refs.extend(batch_refs)
    ctx.append_event(
        "runtime_auto_extracted_jd_details",
        {
            "step_id": ctx.step_id,
            "tool": "extract-observed-job-details-batch",
            "page_count": len(page_ids),
            "artifact_count": len(refs),
        },
    )
    return observations, refs


def auto_fetch_public_urls(
    ctx: RecoveryContext,
    urls: list[str],
    *,
    event_type: str,
    event_payload: dict[str, Any],
) -> tuple[list[ToolObservation], list[dict[str, str]]]:
    if not urls or not ctx.consume_tool_budget():
        return [], []
    observation = ctx.invoke_tool(
        "fetch-public-job-pages",
        {"urls": urls[:10]},
        metadata=dict(ctx.metadata),
    )
    if observation.status != "succeeded":
        return [observation], []
    execution = ExecutorResult(
        status="succeeded",
        observations=[observation],
        summary="已对工具返回的公开岗位链接执行确定性页面核验。",
    )
    refs = ctx.persist(execution, mark="runtime_auto_expand")
    ctx.append_event(event_type, {**event_payload, "artifact_count": len(refs)})
    return [observation], refs

def auto_search_and_fetch(
    ctx: RecoveryContext,
    task_goal: object,
    step_id: str,
) -> tuple[list[ToolObservation], list[dict[str, str]]]:
    """Use the already-authorized public-search fallback once per stall."""
    if not ctx.has_registered_tool("search-public-job-pages"):
        return [], []
    if not isinstance(task_goal, str) or len(task_goal.strip()) < 2:
        return [], []
    attempted_hashes = ctx.metadata.get("public_search_query_hashes", [])
    attempted_hashes = (
        {value for value in attempted_hashes if isinstance(value, str)}
        if isinstance(attempted_hashes, list)
        else set()
    )
    # A model may have already spent the exact goal query before the
    # deterministic recovery runs. Pick the first bounded query variant
    # whose route hash has not been used; this changes only the public
    # search wording, never the source authorization or URL safety rules.
    raw_hints = ctx.metadata.get("discovery_search_hints", [])
    hints = (
        [value.strip() for value in raw_hints if isinstance(value, str) and value.strip()]
        if isinstance(raw_hints, list)
        else []
    )
    query_candidates = tuple(
        dict.fromkeys(
            [
                *hints,
                *discovery_search_hints(task_goal, []),
                task_goal.strip(),
                f"{task_goal.strip()} 岗位详情",
                f"{task_goal.strip()} 官方招聘",
            ]
        )
    )
    queries = [
        candidate[:380]
        for candidate in query_candidates
        if hashlib.sha256(candidate[:380].encode("utf-8")).hexdigest()
        not in attempted_hashes
    ][: max(0, 3 - len(attempted_hashes))]
    search_observations: list[ToolObservation] = []
    search_refs: list[dict[str, str]] = []
    for query in queries:
        if not ctx.consume_tool_budget():
            break
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
        attempted_hashes.add(query_hash)
        search_observation = ctx.invoke_tool(
            "search-public-job-pages",
            {"query": query, "max_results": 5},
            metadata={**ctx.metadata, "runtime_auto_search": True},
        )
        search_observations.append(search_observation)
        search_execution = ExecutorResult(
            status="succeeded",
            observations=[search_observation],
            summary="已使用公开搜索回退核验岗位来源。",
        )
        search_refs.extend(ctx.persist(search_execution))
        if search_observation.status != "succeeded":
            break
        urls = search_result_urls([search_observation])
        if not urls:
            continue
        fetched_observations, fetched_refs = auto_fetch_public_urls(
            ctx,
            urls,
            event_type="runtime_auto_fetched_public_search_results",
            event_payload={"step_id": step_id, "url_count": len(urls)},
        )
        return (
            [*search_observations, *fetched_observations],
            [*search_refs, *fetched_refs],
        )
    return search_observations, search_refs

def recover_discovery_evidence(
    ctx: RecoveryContext,
    observations: list[ToolObservation],
    artifact_refs: list[dict[str, Any]],
) -> tuple[list[ToolObservation], list[dict[str, str]]]:
    """Continue a source-bound discovery route after a model stall.

    Two deterministic recovery cases are safe and common: a list-only
    page needs the existing bounded detail expansion, and a successful
    sheet/search result already contains public URLs that still need
    fetching. Both paths use the registered public fetch tool, preserve
    per-URL failures, and never invent or retry around access controls.
    """
    if any(
        ref.get("artifact_type") == "job_search_results"
        and ref.get("completion_valid") == "true"
        for ref in artifact_refs
    ):
        # A complete official source scan with zero matches is already a
        # final discovery proof. Re-running search cannot create a fetch
        # candidate and only adds route/hash noise before the terminal
        # negative rescue evaluates the original observation.
        return [], []

    # An explicitly named source or exact requested-role evidence page is
    # a hard constraint. Fetch reviewed priority routes before accepting a
    # complete but unrelated page from supplied URLs/search results.
    # Downstream normalization still enforces provenance and captured page
    # status; this never bypasses a captcha or implies an archived JD is open.
    processed_urls = {
        ref.get("source_url")
        for ref in artifact_refs
        if isinstance(ref.get("source_url"), str)
    }
    priority_source_urls = [
        url
        for url in [
            *public_source_mirror_seed_urls(ctx.task_goal),
            *requested_role_seed_urls(ctx.task_goal),
        ]
        if url not in processed_urls
    ]
    priority_observations, priority_refs = auto_fetch_public_urls(
        ctx,
        priority_source_urls,
        event_type="runtime_auto_fetched_priority_source_mirror",
        event_payload={"step_id": ctx.step_id, "url_count": len(priority_source_urls)},
    )
    if any(ref.get("quality") == "jd_complete" for ref in priority_refs):
        return priority_observations, priority_refs
    if contains_access_block(priority_observations):
        return priority_observations, priority_refs
    if priority_observations or priority_refs:
        observations = [*observations, *priority_observations]
        artifact_refs = [*artifact_refs, *priority_refs]

    routing_observations = [
        *observations,
        *ctx.persisted_job_search_observations(artifact_refs),
    ]
    derived_search_hints = discovery_search_hints(
        ctx.task_goal, routing_observations
    )
    existing_search_hints = ctx.metadata.get("discovery_search_hints", [])
    if not isinstance(existing_search_hints, list):
        existing_search_hints = []
    search_ctx = ctx.child(
        {
            **ctx.metadata,
            "discovery_search_hints": list(
                dict.fromkeys(
                    hint
                    for hint in [*existing_search_hints, *derived_search_hints]
                    if isinstance(hint, str) and hint.strip()
                )
            )[:5],
        },
    )

    list_urls = list(
        dict.fromkeys(
            str(ref["source_url"])
            for ref in artifact_refs
            if ref.get("artifact_type") == "public_job_page"
            and ref.get("quality") == "list_only"
            and isinstance(ref.get("source_url"), str)
            and str(ref["source_url"]).startswith(("http://", "https://"))
            and not ref.get("runtime_auto_expand")
        )
    )
    if list_urls:
        expanded_observations, expanded_refs = auto_fetch_public_urls(
            search_ctx,
            list_urls[:3],
            event_type="runtime_auto_expanded_list_pages",
            event_payload={"step_id": ctx.step_id, "url_count": len(list_urls[:3])},
        )
        if any(ref.get("quality") == "jd_complete" for ref in expanded_refs):
            return expanded_observations, expanded_refs
        if contains_access_block(expanded_observations):
            return expanded_observations, expanded_refs
        supplied_urls = ctx.task_context.get("candidate_urls")
        processed_urls = {
            ref.get("source_url")
            for ref in artifact_refs
            if ref.get("artifact_type") == "public_job_page"
            and isinstance(ref.get("source_url"), str)
        }
        if (
            isinstance(supplied_urls, list)
            and supplied_urls
            and not {
                url for url in supplied_urls if isinstance(url, str)
            }.issubset(processed_urls)
        ):
            # Preserve the Executor's source-boundary rule: public search
            # is a fallback only after every supplied candidate has been
            # processed or failed.
            return expanded_observations, expanded_refs
        processed_seed_urls = {
            ref.get("source_url")
            for ref in [*artifact_refs, *expanded_refs]
            if isinstance(ref.get("source_url"), str)
        }
        official_seed_urls = [
            url
            for url in trusted_discovery_seed_urls(
                ctx.task_goal, routing_observations
            )
            if url not in processed_seed_urls
        ]
        seeded_observations, seeded_refs = auto_fetch_public_urls(
            search_ctx,
            official_seed_urls,
            event_type="runtime_auto_fetched_trusted_discovery_seeds",
            event_payload={
                "step_id": ctx.step_id,
                "url_count": len(official_seed_urls),
            },
        )
        seeded_complete = any(
            isinstance(observation.output, dict)
            and any(
                isinstance(page, dict) and page.get("quality") == "jd_complete"
                for page in (
                    observation.output.get("pages")
                    if isinstance(observation.output.get("pages"), list)
                    else [observation.output]
                )
            )
            for observation in seeded_observations
        )
        if seeded_complete or contains_access_block(seeded_observations):
            return (
                [*expanded_observations, *seeded_observations],
                [*expanded_refs, *seeded_refs],
            )
        search_observations, search_refs = auto_search_and_fetch(
            search_ctx,
            ctx.metadata.get("task_goal"),
            ctx.step_id,
        )
        return (
            [
                *expanded_observations,
                *seeded_observations,
                *search_observations,
            ],
            [*expanded_refs, *seeded_refs, *search_refs],
        )

    has_complete_page = any(
        isinstance(observation.output, dict)
        and any(
            isinstance(page, dict) and page.get("quality") == "jd_complete"
            for page in (
                observation.output.get("pages")
                if isinstance(observation.output.get("pages"), list)
                else [observation.output]
            )
        )
        for observation in observations
    )
    if has_complete_page:
        return [], []
    urls: list[str] = []
    for observation in routing_observations:
        output = observation.output if isinstance(observation.output, dict) else {}
        for collection_name in ("results", "records"):
            collection = output.get(collection_name)
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, dict):
                    continue
                for value in public_urls_from_search_item(item):
                    if value not in urls:
                        urls.append(value)
                if len(urls) >= 10:
                    break
            if len(urls) >= 10:
                break
        if len(urls) >= 10:
            break
    if not urls:
        supplied_urls = ctx.task_context.get("candidate_urls")
        if isinstance(supplied_urls, list):
            processed_urls = {
                ref.get("source_url")
                for ref in artifact_refs
                if isinstance(ref.get("source_url"), str)
            }
            urls = list(
                dict.fromkeys(
                    value
                    for value in supplied_urls
                    if isinstance(value, str)
                    and value.startswith(("http://", "https://"))
                    and value not in processed_urls
                )
            )[:10]
    if not urls:
        processed_urls = {
            ref.get("source_url")
            for ref in artifact_refs
            if isinstance(ref.get("source_url"), str)
        }
        official_seed_urls = [
            url
            for url in trusted_discovery_seed_urls(
                ctx.task_goal, routing_observations
            )
            if url not in processed_urls
        ]
        seeded_observations, seeded_refs = auto_fetch_public_urls(
            search_ctx,
            official_seed_urls,
            event_type="runtime_auto_fetched_trusted_discovery_seeds",
            event_payload={
                "step_id": ctx.step_id,
                "url_count": len(official_seed_urls),
            },
        )
        seeded_complete = any(
            isinstance(observation.output, dict)
            and any(
                isinstance(page, dict) and page.get("quality") == "jd_complete"
                for page in (
                    observation.output.get("pages")
                    if isinstance(observation.output.get("pages"), list)
                    else [observation.output]
                )
            )
            for observation in seeded_observations
        )
        if seeded_complete or contains_access_block(seeded_observations):
            return seeded_observations, seeded_refs
        search_observations, search_refs = auto_search_and_fetch(
            search_ctx,
            ctx.metadata.get("task_goal"),
            ctx.step_id,
        )
        return (
            [*seeded_observations, *search_observations],
            [*seeded_refs, *search_refs],
        )
    fetched_observations, fetched_refs = auto_fetch_public_urls(
        ctx,
        urls,
        event_type="runtime_auto_fetched_search_results",
        event_payload={"step_id": ctx.step_id, "url_count": len(urls)},
    )
    # A sheet/search result is only a routing artifact. If every direct
    # link it supplied ended in an empty/blocked page, spend the single
    # bounded public-search fallback on a different public route. This is
    # still source-bound and safe: no blocked URL is retried and only URLs
    # returned by the search adapter may be fetched next.
    has_complete_page = any(
        isinstance(observation.output, dict)
        and any(
            isinstance(page, dict) and page.get("quality") == "jd_complete"
            for page in (
                observation.output.get("pages")
                if isinstance(observation.output.get("pages"), list)
                else [observation.output]
            )
        )
        for observation in fetched_observations
    )
    if not has_complete_page:
        processed_urls = {
            ref.get("source_url")
            for ref in [*artifact_refs, *fetched_refs]
            if isinstance(ref.get("source_url"), str)
        }
        official_seed_urls = [
            url
            for url in trusted_discovery_seed_urls(
                ctx.task_goal, routing_observations
            )
            if url not in processed_urls
        ]
        seeded_observations, seeded_refs = auto_fetch_public_urls(
            search_ctx,
            official_seed_urls,
            event_type="runtime_auto_fetched_trusted_discovery_seeds",
            event_payload={
                "step_id": ctx.step_id,
                "url_count": len(official_seed_urls),
            },
        )
        seeded_complete = any(
            isinstance(observation.output, dict)
            and any(
                isinstance(page, dict) and page.get("quality") == "jd_complete"
                for page in (
                    observation.output.get("pages")
                    if isinstance(observation.output.get("pages"), list)
                    else [observation.output]
                )
            )
            for observation in seeded_observations
        )
        if seeded_complete or contains_access_block(seeded_observations):
            return (
                [*fetched_observations, *seeded_observations],
                [*fetched_refs, *seeded_refs],
            )
        search_observations, search_refs = auto_search_and_fetch(
            search_ctx,
            ctx.metadata.get("task_goal"),
            ctx.step_id,
        )
        return (
            [
                *fetched_observations,
                *seeded_observations,
                *search_observations,
            ],
            [*fetched_refs, *seeded_refs, *search_refs],
        )
    return fetched_observations, fetched_refs