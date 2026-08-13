# Task Plan: Decouple the Agent Runtime from Career Skills

## Goal
Refactor the project so the generic Planner–Executor–Verifier runtime depends on explicit skill contracts and registries, while the career-domain rules remain in skill packages and the current backend behavior stays compatible.

## Phases
- [x] Phase 1: Inspect canonical skill packages, backend adapters, runtime contracts, and test baseline
- [x] Phase 2: Add generic skill-definition and completion-contract primitives
- [x] Phase 3: Move career-specific completion, context, verification, and policy data behind skill definitions
- [ ] Phase 4: Fix deterministic completion gates, typed runtime state, and privacy/error boundaries
- [ ] Phase 5: Refactor step execution and decision schemas where safe
- [ ] Phase 6: Run focused tests, full relevant tests, and document changed/unchanged surfaces

## Priority
- P0: deterministic final contract gate; no-tool completion bypass; model/error redaction
- P1: SkillDefinition boundary; structured step inputs/outputs/dependencies; typed replan state; unified error policy; context projection
- P2: executor prompt slimming; physical model budgets; deeper step-runner decomposition and DAG scheduling

## Decisions
- Treat `skill/` as the canonical Agent Skills package format and `backend/app/services/career_skills/` as the current executable adapter layer.
- Do not require package directory names to equal runtime skill names; use explicit metadata and adapters.
- Preserve current public APIs and existing skill behavior while migrating the runtime incrementally.
- Do not delete or rename existing migrations or skill scripts in this pass.

## Baseline
- Focused runtime/career tests: `77 passed` before this refactor.
- Existing repository worktree changes are unrelated and must be preserved.

## Status
**Phase 2 complete** - generic skill definitions, package discovery, typed step references, strict contract injection, least-privilege context projection, unified error policy, and privacy-safe model logging are implemented. Prompt policy extraction and typed decision unions remain as explicit follow-up work.
