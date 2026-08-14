"""Runtime-neutral contracts for discoverable Agent Skills."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from backend.app.services.agent_runtime.error_policy import (
    ErrorPolicy,
    default_error_policy,
)
from backend.app.services.agent_runtime.schemas import (
    PlanStep,
    ToolObservation,
)

if TYPE_CHECKING:
    from backend.app.services.agent_runtime.tool_registry import ToolRegistry


class VerificationPolicy(StrEnum):
    """Whether a skill's result needs an independent Verifier decision."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    NEVER = "never"


ObservationCheck = Callable[[ToolObservation], bool]


@dataclass(frozen=True)
class ArtifactPort:
    """A runtime-neutral input/output port owned by a Skill adapter."""

    name: str
    artifact_types: frozenset[str] = frozenset()
    required: bool = True


@dataclass(frozen=True)
class CompletionContract:
    """Deterministic evidence contract supplied by a skill, not the runtime."""

    deliverable_tools: frozenset[str]
    description: str = ""
    observation_check: ObservationCheck | None = None

    def accepts(self, observation: ToolObservation) -> bool:
        if observation.status != "succeeded":
            return False
        if observation.tool_name not in self.deliverable_tools:
            return False
        return self.observation_check(observation) if self.observation_check else True


@dataclass(frozen=True)
class SkillDefinition:
    """All domain policy needed by the generic runtime for one skill."""

    name: str
    description: str = ""
    completion_contract: CompletionContract | None = None
    verification_policy: VerificationPolicy = VerificationPolicy.OPTIONAL
    context_keys: frozenset[str] = frozenset()
    execution_policy: str = ""
    package_path: str | None = None
    package_instructions: str = ""
    input_ports: tuple[ArtifactPort, ...] = ()
    output_ports: tuple[ArtifactPort, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SkillRegistry:
    """Immutable-after-startup registry of skill definitions."""

    _ARTIFACT_TYPE_ALIASES = {
        "match_result": "job_matching_report",
    }

    def __init__(
        self,
        definitions: Iterable[SkillDefinition] = (),
        *,
        error_policy: ErrorPolicy | None = None,
    ) -> None:
        self._definitions: dict[str, SkillDefinition] = {}
        self._error_policy = error_policy or default_error_policy()
        for definition in definitions:
            self.register(definition)

    def register(self, definition: SkillDefinition) -> None:
        if not definition.name.strip():
            raise ValueError("skill name must not be empty")
        if definition.name in self._definitions:
            raise ValueError(f"skill already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> SkillDefinition | None:
        return self._definitions.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._definitions)

    @property
    def error_policy(self) -> ErrorPolicy:
        return self._error_policy

    def definitions(self) -> tuple[SkillDefinition, ...]:
        return tuple(self._definitions.values())

    def project_private_context(
        self,
        skill_names: Iterable[str],
        private_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply least-privilege context projection for one step."""
        allowed_keys = frozenset(
            key
            for skill_name in skill_names
            if (definition := self.get(skill_name)) is not None
            for key in definition.context_keys
        )
        return {
            key: value
            for key, value in private_context.items()
            if key in allowed_keys
        }

    def prompt_policy(self, skill_names: Iterable[str], *, max_chars: int = 4_000) -> str:
        """Render bounded policy for every activated Skill.

        The planner commonly receives several Skills at once. A single global
        prefix slice used to let the first, usually largest, package hide all
        later packages. Allocate a bounded slice per Skill so every activated
        package remains visible, while preserving the package head and its
        adapter-boundary tail.
        """
        sections: list[str] = []
        definitions = [
            definition
            for skill_name in skill_names
            if (definition := self.get(skill_name)) is not None
        ]
        if not definitions or max_chars <= 0:
            return ""
        per_skill_chars = max(700, max_chars // len(definitions))
        for definition in definitions:
            contract = definition.completion_contract
            deliverables = (
                ", ".join(sorted(contract.deliverable_tools))
                if contract is not None
                else "none"
            )
            sections.append(
                f"Skill: {definition.name}\n"
                f"Description: {definition.description}\n"
                f"Deliverables: {deliverables}\n"
                f"Accepted artifact inputs: {self._port_types(definition.input_ports)}\n"
                f"Produced artifact outputs: {self._port_types(definition.output_ports)}\n"
                f"Verification: {definition.verification_policy.value}"
                + (f"\nPolicy: {definition.execution_policy}" if definition.execution_policy else "")
                + (
                    "\nCanonical Skill instructions:\n"
                    + _instruction_excerpt(
                        definition.package_instructions,
                        max_chars=max(500, per_skill_chars - 350),
                    )
                    if definition.package_instructions
                    else ""
                )
            )
        rendered = "\n\n".join(sections)
        if len(rendered) <= max_chars:
            return rendered
        # Keep section boundaries intact when the metadata headers consume the
        # available budget. This is a final safety cap, not the normal path.
        return rendered[:max_chars]

    @staticmethod
    def _port_types(ports: Iterable[ArtifactPort]) -> str:
        """Render canonical artifact types so planners do not invent aliases."""
        values = sorted({artifact_type for port in ports for artifact_type in port.artifact_types})
        return ", ".join(values) if values else "none"

    def has_completion_contract(self, step: PlanStep) -> bool:
        """Return true only when every skill in a step declares a contract."""
        return bool(step.allowed_skills) and all(
            (definition := self.get(skill_name)) is not None
            and definition.completion_contract is not None
            for skill_name in step.allowed_skills
        )

    def validate_step_ports(self, step: PlanStep) -> str | None:
        """Return a stable contract error for explicit incompatible ports."""
        definitions = [self.get(name) for name in step.allowed_skills]
        for input_ref in step.inputs:
            if input_ref.kind != "artifact" or not input_ref.artifact_type:
                continue
            accepted = {
                artifact_type
                for definition in definitions
                if definition is not None
                for port in definition.input_ports
                for artifact_type in port.artifact_types
            }
            if accepted and input_ref.artifact_type not in accepted:
                return (
                    f"step {step.step_id} artifact input '{input_ref.name}' has type "
                    f"'{input_ref.artifact_type}', but allowed Skills accept only "
                    f"{sorted(accepted)}"
                )
        for output in step.outputs:
            if not output.artifact_type:
                continue
            accepted = {
                artifact_type
                for definition in definitions
                if definition is not None
                for port in definition.output_ports
                for artifact_type in port.artifact_types
            }
            if accepted and output.artifact_type not in accepted:
                return (
                    f"step {step.step_id} output '{output.name}' has type "
                    f"'{output.artifact_type}', but allowed Skills produce only "
                    f"{sorted(accepted)}"
                )
        return None

    def normalize_step_ports(self, step: PlanStep) -> PlanStep:
        """Canonicalize one known model alias before runtime port validation.

        The alias map is deliberately tiny and one-way. It repairs a naming
        synonym emitted by the model; it never invents an artifact or widens
        a Skill's accepted port contract.
        """
        def canonical(value: str | None) -> str | None:
            return self._ARTIFACT_TYPE_ALIASES.get(value, value)

        outputs = [
            output.model_copy(update={"artifact_type": canonical(output.artifact_type)})
            for output in step.outputs
        ]
        inputs = [
            input_ref.model_copy(
                update={"artifact_type": canonical(input_ref.artifact_type)}
            )
            for input_ref in step.inputs
        ]
        return step.model_copy(update={"inputs": inputs, "outputs": outputs})

    def step_contract_met(
        self, step: PlanStep, observations: Sequence[ToolObservation]
    ) -> bool:
        """Evaluate every skill contract in a multi-skill step."""
        if not self.has_completion_contract(step):
            return False
        return all(
            any(
                definition.completion_contract.accepts(observation)
                for observation in observations
            )
            for skill_name in step.allowed_skills
            if (definition := self.get(skill_name)) is not None
            and definition.completion_contract is not None
        )

    def completion_evidence_gate(
        self,
        step: PlanStep,
        observations: Sequence[ToolObservation],
        *,
        summary: str | None,
        artifact_refs: Sequence[Mapping[str, Any]] = (),
    ) -> bool:
        """Apply a contract using observations and trusted runtime artifacts."""
        return self.completion_evidence_diagnostics(
            step, observations, summary=summary, artifact_refs=artifact_refs
        )["gate_passed"]

    def completion_evidence_diagnostics(
        self,
        step: PlanStep,
        observations: Sequence[ToolObservation],
        *,
        summary: str | None,
        artifact_refs: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Explain the gate without exposing raw evidence or model prose."""
        summary_present = isinstance(summary, str) and bool(summary.strip())
        contract_declared = self.has_completion_contract(step)
        observation_contract_met = self.step_contract_met(step, observations)
        trusted_artifact_contract_met = self._trusted_artifact_contract_met(
            step, artifact_refs
        )
        contract_met = observation_contract_met or trusted_artifact_contract_met
        # A bounded batch may contain both usable public pages and per-URL
        # anti-bot/adapter failures.  Once a trusted deliverable exists, the
        # failed URLs are partial coverage diagnostics, not a reason to discard
        # the valid result.  A blocked-only observation still blocks the gate.
        blocked = self.has_blocked_evidence(observations) and not contract_met
        reason_codes: list[str] = []
        if not summary_present:
            reason_codes.append("missing_summary")
        if contract_declared and not contract_met:
            reason_codes.append("missing_deliverable")
        if blocked:
            reason_codes.append("blocked_evidence")
        if not contract_declared:
            reason_codes.append("no_explicit_contract")
        return {
            "summary_present": summary_present,
            "contract_declared": contract_declared,
            "observation_contract_met": observation_contract_met,
            "trusted_artifact_contract_met": trusted_artifact_contract_met,
            "contract_met": contract_met,
            "blocked": blocked,
            "gate_passed": summary_present and (not contract_declared or contract_met) and not blocked,
            "reason_codes": reason_codes,
            "observation_count": len(observations),
            "trusted_artifact_count": len(artifact_refs),
            "observation_tools": sorted({item.tool_name for item in observations}),
        }

    def _trusted_artifact_contract_met(
        self, step: PlanStep, artifact_refs: Sequence[Mapping[str, Any]]
    ) -> bool:
        """Use only runtime-produced refs; model-proposed URLs never qualify."""
        refs = [item for item in artifact_refs if isinstance(item, Mapping)]
        if not self.has_completion_contract(step):
            return False
        for skill_name in step.allowed_skills:
            definition = self.get(skill_name)
            contract = definition.completion_contract if definition else None
            if contract is not None and not any(
                ref.get("tool") in contract.deliverable_tools
                and self._trusted_artifact_ref_is_usable(ref)
                for ref in refs
            ):
                return False
        declared = {item.artifact_type for item in step.outputs if item.artifact_type}
        produced = {
            str(ref.get("artifact_type"))
            for ref in refs
            if self._trusted_artifact_ref_is_usable(ref)
        }
        checkable = declared & {"public_job_page", "structured_job_details"}
        return not checkable or checkable.issubset(produced)

    @staticmethod
    def _trusted_artifact_ref_is_usable(ref: Mapping[str, Any]) -> bool:
        if not all(
            isinstance(ref.get(key), str) and bool(ref.get(key))
            for key in ("artifact_id", "artifact_type", "tool", "source_url", "content_hash")
        ):
            return False
        artifact_type = ref.get("artifact_type")
        if artifact_type == "public_job_page":
            return ref.get("quality") == "jd_complete"
        if artifact_type == "structured_job_details":
            return ref.get("source_quality") in {None, "jd_complete"}
        return True

    def has_blocked_evidence(self, observations: Sequence[ToolObservation]) -> bool:
        """Inspect error codes and structured output markers deterministically."""
        for observation in observations:
            if self.error_policy.is_blocked(observation.error_code):
                return True
            output = observation.output or {}
            failures = output.get("failures")
            if isinstance(failures, list) and any(
                isinstance(failure, dict)
                and self.error_policy.is_blocked(str(failure.get("error_code") or ""))
                for failure in failures
            ):
                return True
            if _output_blocked(output):
                return True
        return False

    def requires_verification(self, step: PlanStep, complexity: str) -> bool:
        """Let skill risk policy decide verification, with complexity fallback."""
        if any(
            (definition := self.get(skill_name)) is not None
            and definition.verification_policy is VerificationPolicy.REQUIRED
            for skill_name in step.allowed_skills
        ):
            return True
        if any(
            (definition := self.get(skill_name)) is not None
            and definition.verification_policy is VerificationPolicy.NEVER
            for skill_name in step.allowed_skills
        ):
            return False
        return complexity in {"L3", "L4"} or step.requires_verification

    @classmethod
    def from_tool_registry(cls, tools: ToolRegistry) -> "SkillRegistry":
        """Build a compatibility registry from executable tool metadata.

        Legacy tests and third-party integrations do not yet provide explicit
        skill definitions. When no tool declares ``is_deliverable``, all tools
        in that skill are treated as deliverables for backwards compatibility;
        production adapters should set the flag explicitly.
        """
        grouped: dict[str, list[Any]] = {}
        for definition in tools.definitions:
            if definition.skill_name:
                grouped.setdefault(definition.skill_name, []).append(definition)
        definitions: list[SkillDefinition] = []
        for name, tool_definitions in grouped.items():
            explicit = [item.is_deliverable for item in tool_definitions]
            deliverables = {
                item.name
                for item in tool_definitions
                if item.is_deliverable is True
            }
            if not any(value is not None for value in explicit):
                deliverables = {item.name for item in tool_definitions}
            definitions.append(
                SkillDefinition(
                    name=name,
                    description=f"Compatibility definition for {name}",
                    completion_contract=(
                        CompletionContract(frozenset(deliverables))
                        if deliverables
                        else None
                    ),
                )
            )
        return cls(definitions)


def _output_blocked(output: Mapping[str, Any]) -> bool:
    status = output.get("status")
    reason = output.get("reason")
    return status in {"needs_manual_review", "blocked"} or reason in {
        "ocr_disabled",
        "login_required",
        "captcha",
        "anti_bot",
    }


def _instruction_excerpt(instructions: str, *, max_chars: int) -> str:
    """Keep package policy available without injecting references/scripts wholesale."""
    cleaned = " ".join(line.strip() for line in instructions.splitlines() if line.strip())
    if len(cleaned) <= max_chars:
        return cleaned
    head_chars = max_chars // 2
    tail_chars = max_chars - head_chars
    return (
        cleaned[:head_chars]
        + " ... [canonical Skill middle omitted] ... "
        + cleaned[-tail_chars:]
    )
