# Legacy platform archive

This directory contains tests and source material retired from the default
personal-career-assistant product path on 2026-08-02.

The recoverable repository state immediately before retirement is tagged as
`archive/pre-personal-career-assistant-retirement`. The earlier product-only
archive is `archive/pre-personal-career-agent`.

Archived material covered campus job operations, administrator review,
Windows device execution, automatic application preparation, and the
LangGraph/Deep Agents demonstration runtime. It is deliberately excluded from
the default Python test collection and Docker production build. It must not be
reintroduced into `backend/app/main.py`, `backend/app/api/router.py`, or the
personal assistant frontend without a new reviewed design.
