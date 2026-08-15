---
name: application-tracking
description: >
  Track the user's real-world job applications through a state machine (saved -> applied ->
  screening -> interview -> offer / rejected / withdrawn). A non-agent utility skill: validate
  whether a status transition is legal, list the transitions allowed from a status, normalize a
  free-form status string to the canonical token, or enumerate every status. Use when the user
  wants to "记录投递进度", "查投递状态", "判断能否从 screening 转到 interview", "校验投递状态流转",
  or otherwise manage the lifecycle of jobs they have applied to. This skill NEVER files an
  application on the user's behalf - it only advises on whether a move is legal.
compatibility: requires Python 3.10+ (stdlib only - no LLM, no browser, no network)
---

# Application Tracking Skill

Advise on the lifecycle of the user's real-world job applications. Designed as a
pi-agent skill - the LLM (you) answers the user's tracking questions, the
`scripts/track.py` helper enforces the state-machine rules deterministically.

**This file is a dispatch hub.** It is intentionally short. Load the reference
file that matches your task from [Progressive disclosure](#progressive-disclosure-how-deep-to-go)
or [References](#references) - do NOT read them all up front.

## Why this skill exists

Application tracking is the user's personal record of jobs they have applied to
(or plan to). As each application progresses, the user advances it through a
fixed state machine. Two things must always hold:

1. **No illegal skips.** A user cannot jump `saved` straight to `offer`; the
   allowed forward edges are fixed and enforced.
2. **No auto-submit (security gate #1).** The platform never files an
   application on the user's behalf. Every status advance is an explicit human
   action; this skill only *advises* on whether a move is legal. It does not
   touch the backend `ApplicationTrackingService` and makes no network calls.

The single `scripts/track.py` helper is a stdlib-only CLI that mirrors the
backend domain rules inline, so the LLM can validate a transition, list the
moves available from a status, normalize a free-form label, or enumerate the
whole lifecycle without any external dependency.

## State machine

```
saved -> applied -> screening -> interview -> offer
                               \           \           \
                                rejected    rejected    rejected
    \-- withdrawn <-- (any non-terminal state) -- offer
```

- **Non-terminal**: `saved`, `applied`, `screening`, `interview` (the user can keep advancing or abandon).
- **Terminal**: `offer`, `rejected`, `withdrawn` (no further transitions).
- `withdrawn` is reachable from every non-terminal state *and* from `offer` (declining an offer) and is itself terminal.

## Quick start

```bash
# Is saved -> applied a legal move?
python scripts/track.py validate-transition --from saved --to applied

# What can follow "screening"?
python scripts/track.py allowed-transitions --status screening

# Normalize a free-form label (case-insensitive, trims whitespace)
python scripts/track.py normalize-status --status " Interview "

# Enumerate every status + terminal/non-terminal split
python scripts/track.py list-statuses
```

Every subcommand prints a single-line JSON result to stdout and exits 0 (a
`valid=false` or `status=error` outcome is a query result, not a crash). Pass
`--out output/evidence/<name>.json` to also persist the full result for audit.

## Full workflow

The skill has one phase - the LLM (you) decides which subcommand answers the
user's question and reads the JSON result back to them.

| User intent | Subcommand | What it returns |
|-------------|------------|-----------------|
| "Can I go from X to Y?" | `validate-transition --from X --to Y` | `valid` + `from_terminal`/`to_terminal` + `reason` |
| "What can follow X?" | `allowed-transitions --status X` | `transitions` list + `terminal` flag |
| "Is this status string valid?" | `normalize-status --status X` | `normalized` canonical token + `valid` |
| "What are all the statuses?" | `list-statuses` | `statuses` + `terminal` + `non_terminal` |

`validate-transition` returns `status=error` with `code=unknown_from_status` /
`unknown_to_status` when either side is not a recognized status; otherwise
`status=ok` with `valid=true|false`. Terminal statuses admit no transitions
(`allowed-transitions` returns an empty list).

## Error handling

| Situation | Result shape | Exit code |
|-----------|--------------|-----------|
| Unknown `--from` status | `{"status":"error","code":"unknown_from_status",...}` | 0 |
| Unknown `--to` status | `{"status":"error","code":"unknown_to_status",...}` | 0 |
| Unknown `--status` (allowed-transitions) | `{"status":"error","code":"unknown_status",...}` | 0 |
| Unknown status (normalize-status) | `{"status":"ok","normalized":null,"valid":false}` | 0 |
| Legal query, illegal transition | `{"status":"ok","valid":false,...}` | 0 |
| Legal query, legal transition | `{"status":"ok","valid":true,...}` | 0 |

There are no crashes: bad input is always a structured `status=error` result
with exit 0, so the caller (you, the LLM) gets a clean message to relay.

## Security boundary (HARD)

- **Read-only advice.** This skill never writes to a database, never calls a
  network API, and never files or advances an application. It only validates.
- **No auto-submit (security gate #1).** The user alone advances a status; this
  skill's `validate-transition` is advisory, not an action.
- **No secrets.** No API keys, tokens, or credentials are read or emitted.

## Progressive disclosure (how deep to go)

| Level | When | What to load |
|-------|------|---------------|
| L1 | "Can status X go to Y?" / "What follows X?" | This file's [State machine](#state-machine) + [Quick start](#quick-start) |
| L2 | "Explain the full lifecycle / what's terminal" | This file + [references/state-machine.md](references/state-machine.md) |
| L3 | "Why is this transition rejected?" / auditing the rules | This file + [references/state-machine.md](references/state-machine.md) (full transition table + terminal rules) |

## References

- [references/state-machine.md](references/state-machine.md) - Full transition table, terminal/non-terminal rules, and the rationale behind the `withdrawn`-from-anywhere escape hatch.

## Scripts

- `scripts/track.py` - State-machine utility. Subcommands: `validate-transition`, `allowed-transitions`, `normalize-status`, `list-statuses`. Stdlib-only; mirrors `backend.app.domain.application_tracking` inline.
