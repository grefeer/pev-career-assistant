"""Pure policies for top-level evaluation counting and trace classification."""

from __future__ import annotations

from collections import Counter
import re
from typing import Any, Iterable

NON_SUCCESS_STATUSES = frozenset({"failed", "waiting_user", "unknown"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def top_level_records(records: Iterable[object]) -> list[dict[str, Any]]:
    """Keep valid, de-duplicated top-level result records only."""
    unique: dict[str, dict[str, Any]] = {}
    for raw in records:
        if not isinstance(raw, dict):
            continue
        case_id = raw.get("id")
        result = raw.get("result")
        if not isinstance(case_id, str) or not case_id or not isinstance(result, dict):
            continue
        if not isinstance(result.get("status"), str):
            continue
        if (
            raw.get("type") == "chain" and "links" in raw
        ) or re.search(r"-L\d+$", case_id):
            continue
        unique.setdefault(case_id, raw)
    return list(unique.values())


def case_status(record: dict[str, Any]) -> str:
    status = (record.get("result") or {}).get("status")
    return status if status in {"succeeded", "failed", "waiting_user"} else "unknown"


def status_counts(records: Iterable[object]) -> Counter[str]:
    return Counter(case_status(record) for record in top_level_records(records))


def non_success_count(records: Iterable[object]) -> int:
    counts = status_counts(records)
    return sum(counts[status] for status in NON_SUCCESS_STATUSES)


def should_stop(records: Iterable[object], *, limit: int = 30) -> bool:
    return non_success_count(records) > limit


def audit_success_record(record: dict[str, Any]) -> dict[str, Any]:
    """Audit a reported success against the source-backed JD contract.

    The live harness must not count a list/search shell, a blocked page, or a
    structured-only projection as a successful job-discovery result.  This is
    deliberately pure so the same rule can be applied after every live run
    and in offline analysis of old result directories.
    """
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    if result.get("status") != "succeeded":
        return {"status": "not_applicable", "reason": "result_not_succeeded"}
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list):
        return {"status": "inconclusive", "reason": "artifacts_missing"}
    valid_pages = 0
    verified_negative_source_scans = 0
    forbidden: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        quality = artifact.get("quality")
        if quality in {"list_only", "search_empty", "blocked", "empty"}:
            forbidden.append(str(quality))
        if artifact.get("artifact_type") != "public_job_page":
            if (
                artifact.get("artifact_type") == "job_search_results"
                and artifact.get("provider") == "juejin_official_search"
                and artifact.get("source_scope") == "juejin.cn"
                and artifact.get("coverage_complete") is True
                and isinstance(artifact.get("time_window_days"), int)
                and artifact["time_window_days"] > 0
                and isinstance(artifact.get("scanned_result_count"), int)
                and artifact["scanned_result_count"] >= 0
                and artifact.get("matched_result_count") == 0
                and artifact.get("terminal_reason") == "search_empty"
                and artifact.get("result_count") == 0
                and isinstance(artifact.get("source_url"), str)
                and artifact["source_url"].startswith(
                    "https://api.juejin.cn/search_api/v1/search"
                )
                and isinstance(artifact.get("content_hash"), str)
                and _SHA256_RE.fullmatch(artifact["content_hash"]) is not None
            ):
                verified_negative_source_scans += 1
            continue
        if (
            isinstance(artifact.get("artifact_id"), str)
            and artifact["artifact_id"]
            and isinstance(artifact.get("source_url"), str)
            and artifact["source_url"]
            and isinstance(artifact.get("content_hash"), str)
            and _SHA256_RE.fullmatch(artifact["content_hash"]) is not None
            and isinstance(artifact.get("visible_text"), str)
            and artifact["visible_text"].strip()
            and quality == "jd_complete"
        ):
            valid_pages += 1
    if valid_pages:
        return {"status": "passed", "valid_public_job_pages": valid_pages}
    if verified_negative_source_scans:
        return {
            "status": "passed",
            "verified_negative_source_scans": verified_negative_source_scans,
        }
    if forbidden:
        return {
            "status": "failed",
            "reason": "forbidden_artifact_quality",
            "qualities": sorted(set(forbidden)),
        }
    return {"status": "inconclusive", "reason": "no_complete_public_job_page"}


def failure_trace(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract a bounded, non-sensitive trace from a result record."""
    trace: list[dict[str, Any]] = []
    for turn in record.get("turns", []):
        if not isinstance(turn, dict):
            continue
        error_code = turn.get("error_code")
        decision = turn.get("verification_decision")
        if error_code or decision in {"RETRY_EXECUTOR", "REPLAN", "NEED_USER"}:
            trace.append({
                "role": turn.get("role"),
                "tool": turn.get("tool_name"),
                "error_code": error_code,
                "verification_decision": decision,
                "turn_index": turn.get("turn_index"),
            })
    for tool in record.get("tool_calls", []):
        if not isinstance(tool, dict) or not tool.get("error_codes"):
            continue
        trace.append({
            "role": "executor",
            "tool": tool.get("tool_name"),
            "error_codes": list(tool.get("error_codes", []))[:8],
            "failed": tool.get("failed", 0),
        })
    return trace[-40:]


def root_cause(record: dict[str, Any]) -> str:
    """Prefer the actionable cause over a terminal budget symptom."""
    contract = record.get("terminal_contract")
    if isinstance(contract, dict) and isinstance(contract.get("failure_class"), str):
        return contract["failure_class"]
    trace = failure_trace(record)
    codes = {
        code
        for item in trace
        for code in item.get("error_codes", [])
        if isinstance(code, str)
    }
    codes.update(
        item["error_code"]
        for item in trace
        if isinstance(item.get("error_code"), str)
    )
    if codes & {"duplicate_tool_call", "candidate_urls_already_supplied"}:
        return "no_progress_duplicate"
    if codes & {"invalid_tool_input", "tool_skill_forbidden", "unknown_tool"}:
        return "contract_or_policy_error"
    if codes & {
        "sheet_rate_limited", "sheet_call_failed", "search_empty",
        "public_search_failed", "public_fetch_failed",
    }:
        return "upstream_tool_failure"
    if codes & {
        "anti_bot_challenge", "captcha", "login_required", "access_denied",
        "domain_temporarily_blocked",
    }:
        return "external_blocked"
    if record.get("result", {}).get("error_code") == "replan_budget_exhausted":
        return "budget_exhausted"
    return "model_or_verifier_decision"
