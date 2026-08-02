"""Full-crawl pipeline primitives (Planner-Executor-Verifier gray migration).

Deterministic crawl orchestration and coverage proof. The LLM/Agent has no
authority to declare a crawl complete; only a positive ``CoverageDecision``
from ``crawling.coverage.verify_coverage`` does.

See docs/superpowers/specs/2026-07-22-job-discovery-complete-crawl-refactor.md.
"""
