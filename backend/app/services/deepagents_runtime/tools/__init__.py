"""Skill-tool adapters for the deep-agents runtime.

The PEV runtime never offers raw skill functions to a deep agent; instead the
harness asks this package for a per-skill tool set (``build_skill_tools``),
carrying the run's ``ToolContext`` via a ``ContextVar`` so skill wrappers can
enforce user scoping and the duplicate-call tracker.

P1 ships the deterministic seam (task 3 of the plan replaces this stub with
the real wrappers).
"""
