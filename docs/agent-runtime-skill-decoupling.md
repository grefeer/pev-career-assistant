# Agent Runtime / Skill Decoupling

This document records the target boundary for the runtime refactor. It is intentionally implementation-facing and will be updated as the migration lands.

## Target boundary

The generic runtime owns planning/execution orchestration, budgets, lifecycle transitions, persistence coordination, loop guards, and final invariant enforcement. A skill owns its name, tool bindings, context projection, execution instructions, completion contract, verification policy, retry/error policy, and artifact types.

```text
Canonical skill package + backend tool adapter
                    |
                    v
            SkillDefinition registry
                    |
                    v
Generic Planner / Runtime / Executor / Verifier
```

## Compatibility rule

Existing career skills remain callable through the current backend registry while their metadata is migrated into `SkillDefinition`. The migration must not require public API callers to know whether a skill came from a package, a Python adapter, or both.

The application composition root injects the strict registry. Embedders that do
not inject one remain in an explicit legacy compatibility mode until they adopt
the contract API; the generic runtime never infers business contracts from tool
names by default.

## Acceptance criteria

- A runtime module can execute a registered non-career skill without importing career skill modules or matching on career-specific names.
- Every registered skill with a completion contract is checked deterministically, even if the executor made zero tool calls.
- A verifier PASS cannot bypass a failed or blocked deterministic contract.
- Skill context projection is explicit and least-privilege.
- Plan steps can declare typed context/artifact inputs, outputs, and earlier-step dependencies.
- Existing focused runtime and career-skill tests continue to pass, with new tests for the decoupling boundary.
