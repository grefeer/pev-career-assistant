"""Unit tests for the single-source completeness proof registry.

These tests pin the closed contract behavior of personalized discovery v1:

* The production registry is empty, so legacy executors never yield a proof.
* Only registered contracts matched by adapter id + URL pattern can be proven.
* A proof requires (a) a JD body on the candidate, (b) at least one evidence
  content hash, (c) no execution error / block reason, and (d) a non-legacy
  executor type.
"""

from __future__ import annotations

from backend.app.services.job_discovery.schemas import (
    CrawlCoverage,
    NormalizedJobCandidate,
    PageEvidence,
    PaginationType,
    DiscoveryRunResult,
)
from backend.app.services.job_discovery.single_source_proof import (
    PRODUCTION_REGISTRY,
    SingleSourceContract,
    SingleSourceProof,
    SingleSourceProofRegistry,
    evaluate_single_source_proof,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def _candidate(
    *,
    title: str = "AI 应用开发工程师",
    company: str = "DJI",
    with_jd_body: bool = True,
    apply_url: str = "https://app.mokahr.com/apply/abc",
) -> NormalizedJobCandidate:
    if with_jd_body:
        description_text = "负责 AI 应用架构设计与落地。"
        responsibilities = "需求拆解；方案设计"
        requirements = "Python；LangChain"
    else:
        description_text = ""
        responsibilities = ""
        requirements = ""
    return NormalizedJobCandidate(
        title=title,
        company_name=company,
        department="大疆创新",
        description_text=description_text,
        responsibilities=responsibilities,
        requirements=requirements,
        locations=["深圳"],
        recruitment_types=["校招"],
        industries=["机器人"],
        apply_url=apply_url,
        confidence=0.9,
        evidence_refs=[],
        normalization_warnings=[],
    )


def _evidence(content_hash: str = "h1") -> PageEvidence:
    return PageEvidence(
        evidence_type="jd_text",
        url="https://x/y",
        content_hash=content_hash,
        text_excerpt="snippet",
    )


def _result(
    *,
    status: str = "succeeded",
    candidates: list[NormalizedJobCandidate] | None = None,
    evidence: list[dict] | None = None,
    block_reason: str | None = None,
    execution_error: str | None = None,
    coverage_complete: bool | None = True,
    summary: str = "ok",
) -> DiscoveryRunResult:
    return DiscoveryRunResult(
        status=status,
        block_reason=block_reason,
        evidence=evidence if evidence is not None else [_evidence()],
        candidates=candidates if candidates is not None else [_candidate()],
        summary=summary,
        coverage=None,  # populated below if requested
        execution_error=execution_error,
    )


class _Task:
    """Minimal stand-in for JobDiscoveryTask used by the evaluator."""

    def __init__(
        self,
        *,
        source_key: str = "dji",
        source_url: str = "https://app.mokahr.com/m/campus-recruitment/dji/143359",
        agent_version: str = "v1",
    ) -> None:
        self.source_key = source_key
        self.source_url = source_url
        self.agent_version = agent_version


def _registry() -> SingleSourceProofRegistry:
    reg = SingleSourceProofRegistry()
    reg.register(
        SingleSourceContract(
            contract_id="dji-mokahr",
            adapter_id="mokahr",
            source_url_pattern="mokahr.com",
            terminal_signal="job_list_complete",
            application_hosts=["app.mokahr.com"],
        )
    )
    return reg


# ─── Production registry is closed ────────────────────────────────────────────


def test_production_registry_is_empty():
    """The production registry ships empty — no source is admitted yet."""
    assert list(PRODUCTION_REGISTRY._contracts) == []  # noqa: SLF001
    assert PRODUCTION_REGISTRY.match("mokahr", "https://app.mokahr.com/x") is None


def test_legacy_executor_types_never_produce_a_proof():
    """PATH C / supervisor / fallback / unknown executors cannot be proven."""
    task = _Task()
    result = _result()
    reg = _registry()
    for executor_type in ("supervisor", "partial_fallback", "unknown"):
        assert (
            evaluate_single_source_proof(task, result, executor_type, registry=reg)
            is None
        )


def test_legacy_executors_short_circuit_before_registry_lookup():
    """A legacy executor must not even consult the registry."""
    task = _Task()
    result = _result()
    calls = {"count": 0}

    class _SpyRegistry:
        def match(self, source_url, executor_type):  # noqa: ARG002
            calls["count"] += 1
            return None

    evaluate_single_source_proof(task, result, "supervisor", registry=_SpyRegistry())
    assert calls["count"] == 0


# ─── Admission conditions ─────────────────────────────────────────────────────


def test_no_contract_registered_returns_none():
    """An executor type with no registered contract cannot be proven."""
    task = _Task()
    result = _result()
    reg = _registry()
    # No contract has adapter_id == "feishu" in the registry.
    assert evaluate_single_source_proof(task, result, "feishu", registry=reg) is None


def test_contract_match_but_no_jd_body_returns_none():
    """A candidate with an empty JD body cannot prove completeness."""
    task = _Task()
    result = _result(candidates=[_candidate(with_jd_body=False)])
    reg = _registry()
    assert evaluate_single_source_proof(task, result, "mokahr", registry=reg) is None


def test_contract_match_but_no_evidence_returns_none():
    """Without evidence content hashes there is no proof."""
    task = _Task()
    result = _result(evidence=[])
    reg = _registry()
    assert evaluate_single_source_proof(task, result, "mokahr", registry=reg) is None


def test_contract_match_but_execution_error_returns_none():
    task = _Task()
    result = _result(execution_error="boom")
    reg = _registry()
    assert evaluate_single_source_proof(task, result, "mokahr", registry=reg) is None


def test_contract_match_but_block_reason_returns_none():
    task = _Task()
    result = _result(status="needs_manual_review", block_reason="login_required")
    reg = _registry()
    assert evaluate_single_source_proof(task, result, "mokahr", registry=reg) is None


def test_contract_match_but_failed_status_returns_none():
    task = _Task()
    result = _result(status="failed")
    reg = _registry()
    assert evaluate_single_source_proof(task, result, "mokahr", registry=reg) is None


def test_registry_none_returns_none():
    """When no registry is wired in (legacy deployment), never proven."""
    task = _Task()
    result = _result()
    assert evaluate_single_source_proof(task, result, "mokahr", registry=None) is None


# ─── Happy path ───────────────────────────────────────────────────────────────


def test_happy_path_emits_proof():
    task = _Task()
    result = _result()
    reg = _registry()
    proof = evaluate_single_source_proof(task, result, "mokahr", registry=reg)
    assert isinstance(proof, SingleSourceProof)
    assert proof.contract_id == "dji-mokahr"
    assert proof.terminal_signal == "job_list_complete"
    assert proof.application_hosts == ["app.mokahr.com"]
    assert proof.evidence_hash  # non-empty hex digest
    # to_dict round-trips with the expected key set.
    payload = proof.to_dict()
    assert set(payload.keys()) == {
        "contract_id",
        "evidence_hash",
        "terminal_signal",
        "application_hosts",
    }


def test_partial_success_still_admissible_when_coverage_complete():
    """``partial_success`` is admissible iff coverage is complete or absent."""
    task = _Task()
    result = _result(status="partial_success")
    reg = _registry()
    assert evaluate_single_source_proof(task, result, "mokahr", registry=reg) is not None


def test_partial_success_inadmissible_when_coverage_incomplete():
    """If a coverage object is attached and reports incomplete, refuse."""
    task = _Task()
    result = _result(status="partial_success")
    result.coverage = CrawlCoverage(
        pagination_type=PaginationType.LOAD_MORE,
        coverage_complete=False,
        incomplete_reason="timeout",
    )
    reg = _registry()
    assert evaluate_single_source_proof(task, result, "mokahr", registry=reg) is None


def test_evidence_hash_is_deterministic_for_same_evidence():
    task = _Task()
    evidence = [_evidence("h1"), _evidence("h2")]
    result = _result(evidence=evidence)
    reg = _registry()
    p1 = evaluate_single_source_proof(task, result, "mokahr", registry=reg)
    p2 = evaluate_single_source_proof(task, result, "mokahr", registry=reg)
    assert p1 is not None and p2 is not None
    assert p1.evidence_hash == p2.evidence_hash


def test_evidence_hash_changes_when_evidence_changes():
    task = _Task()
    reg = _registry()
    p1 = evaluate_single_source_proof(
        task, _result(evidence=[_evidence("h1")]), "mokahr", registry=reg
    )
    p2 = evaluate_single_source_proof(
        task, _result(evidence=[_evidence("h2")]), "mokahr", registry=reg
    )
    assert p1 is not None and p2 is not None
    assert p1.evidence_hash != p2.evidence_hash


def test_match_requires_url_pattern_substring():
    """``source_url_pattern`` must be a substring of the task source URL."""
    task = _Task(source_url="https://example.com/elsewhere")
    result = _result()
    reg = _registry()
    # adapter matches but URL pattern does not → no contract.
    assert evaluate_single_source_proof(task, result, "mokahr", registry=reg) is None


# ─── to_dict contract ─────────────────────────────────────────────────────────


def test_to_dict_is_json_serializable_and_includes_application_hosts():
    import json

    task = _Task()
    result = _result()
    reg = _registry()
    proof = evaluate_single_source_proof(task, result, "mokahr", registry=reg)
    assert proof is not None
    payload = json.loads(json.dumps(proof.to_dict()))
    assert payload["application_hosts"] == ["app.mokahr.com"]
    assert payload["terminal_signal"] == "job_list_complete"
