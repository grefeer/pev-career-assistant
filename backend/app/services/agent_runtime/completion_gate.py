"""Deterministic step-completion gate replacing the LLM Verifier role.

The adaptive runtime no longer asks a model to independently verify a step:
every terminal decision (PASS / RETRY_EXECUTOR / NEED_USER / FAIL) is derived
from the Skill registry's deterministic completion contract plus hard budget
state. The verdict shape mirrors the legacy VerifierResult so the harness
routing branches stay unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from backend.app.domain.agent_runtime import VerificationDecision
from backend.app.services.agent_runtime.skill_definition import SkillRegistry


@dataclass(frozen=True)
class GateVerdict:
    """Deterministic terminal verdict for one step's execution."""

    decision: VerificationDecision
    feedback: str
    error_code: str | None = None


def evaluate_completion_gate(
    *,
    skills: SkillRegistry,
    step: Any,
    observations: Sequence[Any],
    artifact_refs: Sequence[Mapping[str, Any]],
    summary: str | None,
    deadline: float | None = None,
) -> GateVerdict:
    """Evaluate the step's completion contract and return a terminal verdict.

    Decision mapping (mirrors the retired LLM Verifier routing):
    - wall-clock budget expired  -> FAIL / wall_clock_budget_exhausted
    - contract satisfied and unblocked -> PASS
    - blocked evidence -> NEED_USER (never retried around access controls)
    - deliverable present but terminal summary missing -> NEED_USER
    - contract not satisfied, unblocked -> RETRY_EXECUTOR (bounded retry loop)
    """
    if deadline is not None and time.monotonic() >= deadline:
        return GateVerdict(
            decision=VerificationDecision.FAIL,
            feedback="Wall-clock budget exhausted before verification.",
            error_code="wall_clock_budget_exhausted",
        )
    diagnostics = skills.completion_evidence_diagnostics(
        step, observations, summary=summary, artifact_refs=artifact_refs
    )
    if diagnostics["gate_passed"]:
        return GateVerdict(
            decision=VerificationDecision.PASS,
            feedback="确定性交付契约已满足。",
        )
    if skills.has_blocked_evidence(observations):
        # Login/captcha/anti-bot blocks are policy hand-offs: the harness
        # never re-invokes around them and never treats them as retryable.
        return GateVerdict(
            decision=VerificationDecision.NEED_USER,
            feedback="步骤证据被访问限制阻断（登录/验证码/反爬等），请人工确认。",
        )
    if diagnostics["contract_met"] and not diagnostics["summary_present"]:
        return GateVerdict(
            decision=VerificationDecision.NEED_USER,
            feedback="交付物满足契约但终端总结缺失，请人工确认产出。",
        )
    return GateVerdict(
        decision=VerificationDecision.RETRY_EXECUTOR,
        feedback="交付物未满足确定性交付契约，请补充工具产出后重试。",
    )
