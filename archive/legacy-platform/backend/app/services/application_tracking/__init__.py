"""Application-tracking skill - user-scoped job-application progress tracker.

A non-agent skill: the user records the jobs they have applied to (or plan to)
and advances each through the state machine
(saved -> applied -> screening -> interview -> offer / rejected / withdrawn).
No crawl, no LLM, and no auto-submit (security gate #1) - every status advance
is an explicit human action recorded in an append-only event log.
"""
