#!/usr/bin/env python3
"""track.py - Application-tracking state-machine utility for the application-tracking skill.

Self-contained mirror of ``backend.app.domain.application_tracking``. Validates
and normalizes application status transitions for the user-scoped, *non*-agent
application-tracking skill. There is no LLM and no crawl here - and, critically,
**no auto-submit** (security gate #1): the platform never files an application on
the user's behalf. This script only *advises* on whether a transition is legal;
every status advance is an explicit human action recorded by the backend
``ApplicationTrackingService`` (which this skill does not touch).

State machine::

    saved -> applied -> screening -> interview -> offer
                                   \\           \\           \\
                                    rejected    rejected    rejected
        \\-- withdrawn <-- (any non-terminal state) -- offer

``saved``/``applied``/``screening``/``interview`` are non-terminal.
``offer``/``rejected``/``withdrawn`` are terminal.  ``withdrawn`` is reachable
from every non-terminal state (and from ``offer`` - declining an offer).

Usage:
  # Is saved -> applied a legal move?
  python scripts/track.py validate-transition --from saved --to applied \\
      [--out output/evidence/transition.json]

  # What can follow "screening"?
  python scripts/track.py allowed-transitions --status screening [--out ...]

  # Normalize a free-form status string to the canonical token
  python scripts/track.py normalize-status --status " Interview " [--out ...]

  # Enumerate every status + terminal/non-terminal split
  python scripts/track.py list-statuses [--out ...]

Exit code is always 0 (a ``valid=false`` result is a query outcome, not a
crash); the structured result is printed to stdout and, when ``--out`` is given,
written there too. ``--out`` must be a path under ``output/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════
# State machine (mirrors backend.app.domain.application_tracking EXACTLY)
# ═══════════════════════════════════════════════════════════════════

#: Canonical lifecycle tokens for one tracked application.
STATUSES: tuple[str, ...] = (
    "saved",
    "applied",
    "screening",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
)

#: Terminal states: no further transitions are allowed once reached.
TERMINAL_STATUSES: frozenset[str] = frozenset({"offer", "rejected", "withdrawn"})

#: Allowed forward transitions. ``withdrawn`` is reachable from every
#: non-terminal state (plus ``offer``, for declining an offer); ``rejected``
#: is reachable from the active pipeline states.  Terminal states deliberately
#: have no entry - no transitions out.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "saved": frozenset({"applied", "withdrawn"}),
    "applied": frozenset({"screening", "rejected", "withdrawn"}),
    "screening": frozenset({"interview", "rejected", "withdrawn"}),
    "interview": frozenset({"offer", "rejected", "withdrawn"}),
    "offer": frozenset({"withdrawn"}),
}

#: All recognized statuses as a set, for fast membership tests.
_ALL_STATUSES: frozenset[str] = frozenset(STATUSES)


def normalize_status(status: str | None) -> str | None:
    """Normalize a free-form status string to the canonical token.

    Case-insensitive, trims surrounding whitespace. Returns ``None`` when the
    input does not match any known status.
    """
    if not isinstance(status, str):
        return None
    candidate = status.strip().lower()
    return candidate if candidate in _ALL_STATUSES else None


def is_terminal(status: str) -> bool:
    """Return True when ``status`` admits no further transition."""
    return status in TERMINAL_STATUSES


def allowed_transitions(status: str) -> frozenset[str]:
    """Return the set of statuses ``status`` may legally transition to."""
    return _TRANSITIONS.get(status, frozenset())


def is_valid_transition(from_status: str, to_status: str) -> bool:
    """Return True when ``from_status`` -> ``to_status`` is a legal move."""
    return to_status in allowed_transitions(from_status)


# ═══════════════════════════════════════════════════════════════════
# Output helpers
# ═══════════════════════════════════════════════════════════════════

def _emit(result: dict[str, Any], out_path: str | None) -> None:
    """Write the full result to ``--out`` (if given) and print it to stdout."""
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


def _error(code: str, message: str, out_path: str | None) -> int:
    """Emit a structured error result (exit 0 - soft failure for the caller)."""
    _emit({"status": "error", "code": code, "message": message}, out_path)
    return 0


# ═══════════════════════════════════════════════════════════════════
# Subcommands
# ═══════════════════════════════════════════════════════════════════

def cmd_validate_transition(from_raw: str, to_raw: str, out_path: str | None) -> int:
    """Validate a from->to transition, emitting the structured verdict."""
    from_status = normalize_status(from_raw)
    to_status = normalize_status(to_raw)
    if from_status is None:
        return _error("unknown_from_status", f"unknown source status: {from_raw!r}", out_path)
    if to_status is None:
        return _error("unknown_to_status", f"unknown target status: {to_raw!r}", out_path)

    valid = is_valid_transition(from_status, to_status)
    reason = (
        "legal transition"
        if valid
        else f"{from_status} -> {to_status} is not an allowed transition"
    )
    result = {
        "status": "ok",
        "valid": valid,
        "from": from_status,
        "to": to_status,
        "from_terminal": is_terminal(from_status),
        "to_terminal": is_terminal(to_status),
        "reason": reason,
    }
    _emit(result, out_path)
    return 0


def cmd_allowed_transitions(status_raw: str, out_path: str | None) -> int:
    """List the statuses ``status`` may legally transition to."""
    status = normalize_status(status_raw)
    if status is None:
        return _error("unknown_status", f"unknown status: {status_raw!r}", out_path)
    transitions = sorted(allowed_transitions(status))
    result = {
        "status": "ok",
        "status_value": status,
        "terminal": is_terminal(status),
        "transitions": transitions,
    }
    _emit(result, out_path)
    return 0


def cmd_normalize_status(status_raw: str, out_path: str | None) -> int:
    """Normalize a free-form status string to the canonical token."""
    normalized = normalize_status(status_raw)
    result = {
        "status": "ok",
        "input": status_raw,
        "normalized": normalized,
        "valid": normalized is not None,
    }
    _emit(result, out_path)
    return 0


def cmd_list_statuses(out_path: str | None) -> int:
    """Enumerate every status, split into terminal and non-terminal sets."""
    terminal = [s for s in STATUSES if is_terminal(s)]
    non_terminal = [s for s in STATUSES if not is_terminal(s)]
    result = {
        "status": "ok",
        "statuses": list(STATUSES),
        "terminal": terminal,
        "non_terminal": non_terminal,
    }
    _emit(result, out_path)
    return 0


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def _add_out(parser: argparse.ArgumentParser) -> None:
    """Add the optional ``--out`` flag to one subparser.

    Defined per-subparser (not on the parent) so ``--out`` is recognized after
    the subcommand, the way an agent invokes it:
    ``track validate-transition --from saved --to applied --out output/evidence/t.json``.
    """
    parser.add_argument(
        "--out",
        default=None,
        help="Optional output path (must be under output/) for the full result JSON",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and normalize application-tracking status transitions."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate-transition", help="Validate a from->to transition")
    p_validate.add_argument("--from", dest="from_status", required=True, help="Source status")
    p_validate.add_argument("--to", dest="to_status", required=True, help="Target status")
    _add_out(p_validate)

    p_allowed = sub.add_parser("allowed-transitions", help="List allowed next statuses")
    p_allowed.add_argument("--status", dest="status", required=True, help="Current status")
    _add_out(p_allowed)

    p_norm = sub.add_parser("normalize-status", help="Normalize a status string")
    p_norm.add_argument("--status", dest="status", required=True, help="Status to normalize")
    _add_out(p_norm)

    p_list = sub.add_parser("list-statuses", help="List all statuses (terminal/non-terminal)")
    _add_out(p_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate-transition":
        return cmd_validate_transition(args.from_status, args.to_status, args.out)
    if args.command == "allowed-transitions":
        return cmd_allowed_transitions(args.status, args.out)
    if args.command == "normalize-status":
        return cmd_normalize_status(args.status, args.out)
    if args.command == "list-statuses":
        return cmd_list_statuses(args.out)
    # argparse(required=True) makes this unreachable, but keep a safe default.
    return _error("unknown_command", f"unknown command: {args.command!r}", args.out)  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover - script entry, exercised by the smoke test
    sys.exit(main())
