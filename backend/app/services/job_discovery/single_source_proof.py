"""Single-resource completeness proof for personalized discovery v1 (Task 4).

The personalized-discovery gate accepts a retained task as "complete" through
one of two proofs:

1. Full PEV crawl coverage (``coverage_verified`` from ``verify_coverage``) -
   set by the Planner-Executor-Verifier pipeline for the four migrated
   complete-crawl adapters (Moka / Feishu / Inovance / Xiaohongshu).
2. A registered **single-source complete** contract - for a public resource
   that exposes exactly one JD detail with no pagination (one page, one
   detail). This module is the admission mechanism for that second proof.

The v1 production registry is intentionally EMPTY: no single-resource source
is registered yet. Unit tests inject a fixture-only registry to prove the
admission mechanism; this does NOT enable a production source. Registering
a real source is a separate, evidence-reviewed product decision (Task 8
checklist). Generic WeChat, PDD, SnapshotExecutor, Alibaba SPA, and all
legacy/PATH C categories are never registered.

A proof is emitted only when EVERY positive signal is present:
  * the executor is an adapter/snapshot path (legacy supervisor / fallback
    never get a proof - PATH C is never complete by implication);
  * a contract matches the task's source URL + executor type;
  * the result carries non-empty JD text on at least one candidate;
  * the result carries at least one ``PageEvidence`` SHA-256 content hash;
  * no execution error, block reason, or wall (``needs_manual_review``);
  * no pagination continuation (single-page: ``coverage is None``; multi-page:
    ``coverage.coverage_complete`` is True).

The proof is a sanitized, closed contract - it never carries raw page text,
URLs beyond the allowlist, cookies, tokens, or wall text. The worker
serializes it into ``result_summary_json["single_source_complete"]`` so the
personalized service can gate on it without touching raw payloads.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.db.models import JobDiscoveryTask
    from backend.app.services.job_discovery.schemas import DiscoveryRunResult


@dataclass(frozen=True)
class SingleSourceContract:
    """A registered single-resource completeness contract.

    ``adapter_id`` matches the worker ``executor_type`` ("adapter" / "snapshot").
    ``source_url_pattern`` is a substring matched against ``task.source_url``.
    ``terminal_signal`` is the exact completion signal this contract certifies.
    ``application_hosts`` is the closed allowlist for delivered apply URLs.
    """

    contract_id: str
    adapter_id: str
    source_url_pattern: str
    terminal_signal: str
    application_hosts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SingleSourceProof:
    """A sanitized, serialized completeness proof written to the task summary."""

    contract_id: str
    evidence_hash: str
    terminal_signal: str
    application_hosts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# Legacy PATH C / fallback executors are never complete by implication - they
# carry no deterministic coverage and must not receive a single-source proof.
_LEGACY_EXECUTORS = frozenset({"supervisor", "partial_fallback", "unknown"})


class SingleSourceProofRegistry:
    """Closed registry of single-resource contracts.

    ``PRODUCTION_REGISTRY`` is empty in v1. Tests inject a fixture registry to
    exercise the admission mechanism without enabling a production source.
    """

    def __init__(self, contracts: list[SingleSourceContract] | None = None) -> None:
        self._contracts: list[SingleSourceContract] = list(contracts or [])

    def register(self, contract: SingleSourceContract) -> None:
        self._contracts.append(contract)

    def match(
        self, source_url: str, executor_type: str
    ) -> SingleSourceContract | None:
        url = source_url or ""
        for contract in self._contracts:
            if (
                contract.adapter_id == executor_type
                and contract.source_url_pattern
                and contract.source_url_pattern in url
            ):
                return contract
        return None


PRODUCTION_REGISTRY = SingleSourceProofRegistry()


def _candidate_has_jd_body(candidate: object) -> bool:
    resp = getattr(candidate, "responsibilities", "") or ""
    req = getattr(candidate, "requirements", "") or ""
    if isinstance(candidate, dict):
        resp = candidate.get("responsibilities") or ""
        req = candidate.get("requirements") or ""
    return bool(str(resp).strip() or str(req).strip())


def evaluate_single_source_proof(
    task: JobDiscoveryTask,
    result: DiscoveryRunResult,
    executor_type: str,
    *,
    registry: SingleSourceProofRegistry | None = None,
) -> SingleSourceProof | None:
    """Return a proof iff every positive signal is present, else ``None``."""
    if registry is None or executor_type in _LEGACY_EXECUTORS:
        return None
    contract = registry.match(task.source_url, executor_type)
    if contract is None:
        return None
    # Non-empty JD text on at least one candidate.
    candidates = getattr(result, "candidates", None) or []
    if not any(_candidate_has_jd_body(c) for c in candidates):
        return None
    # At least one PageEvidence SHA-256 hash.
    evidence = getattr(result, "evidence", None) or []
    hashes = [
        getattr(e, "content_hash", "") or ""
        for e in evidence
        if getattr(e, "content_hash", "")
    ]
    if not hashes:
        return None
    # No execution error / block / wall.
    if getattr(result, "execution_error", None):
        return None
    if getattr(result, "block_reason", None):
        return None
    if getattr(result, "status", None) not in ("succeeded", "partial_success"):
        return None
    # No pagination continuation: single-page (coverage is None) or complete.
    coverage = getattr(result, "coverage", None)
    if coverage is not None and not getattr(coverage, "coverage_complete", False):
        return None
    return SingleSourceProof(
        contract_id=contract.contract_id,
        evidence_hash=hashes[0],
        terminal_signal=contract.terminal_signal,
        application_hosts=list(contract.application_hosts),
    )
