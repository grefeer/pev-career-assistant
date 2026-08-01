# Application-Tracking State Machine

The canonical rules `scripts/track.py` enforces. These mirror
`backend.app.domain.application_tracking` exactly; if the two ever disagree, the
backend domain is authoritative and this skill must be re-synced.

## Statuses

| Status | Meaning | Terminal? |
|--------|---------|-----------|
| `saved` | User saved a job to apply to later | no |
| `applied` | User submitted the application | no |
| `screening` | Recruiter/ATS is reviewing the application | no |
| `interview` | Candidate is in the interview loop | no |
| `offer` | An offer has been extended (and may be declined) | yes |
| `rejected` | The application was turned down | yes |
| `withdrawn` | The user abandoned the application | yes |

## Allowed forward transitions

| From | To |
|------|----|
| `saved` | `applied`, `withdrawn` |
| `applied` | `screening`, `rejected`, `withdrawn` |
| `screening` | `interview`, `rejected`, `withdrawn` |
| `interview` | `offer`, `rejected`, `withdrawn` |
| `offer` | `withdrawn` |
| `rejected` | _(none - terminal)_ |
| `withdrawn` | _(none - terminal)_ |

## Rules

1. **No transitions out of a terminal state.** `offer`, `rejected`, and
   `withdrawn` have no outgoing edges. `allowed-transitions` returns `[]` and
   `validate-transition` returns `valid=false` for any move out of them.

2. **`withdrawn` is the universal escape hatch.** It is reachable from every
   non-terminal state (`saved`, `applied`, `screening`, `interview`) *and* from
   `offer` (the user declines an offer). This lets the user abandon an
   application at any point without forcing a fake forward step first.

3. **`rejected` is reachable from the active pipeline.** The pipeline states
   `applied`, `screening`, and `interview` can each transition to `rejected`
   (a rejection can happen at any of those stages). `saved` cannot be rejected
   directly - nothing has been submitted yet, so the user either applies
   (`applied`) or abandons (`withdrawn`).

4. **No backward transitions.** Once `applied`, the record cannot return to
   `saved`; once `screening`, it cannot return to `applied`. Progress is
   monotonic forward (or `withdrawn`). This matches reality: you cannot un-submit.

5. **Normalization is case-insensitive and trims whitespace.** `" Interview "`,
   `"INTERVIEW"`, and `"interview"` all normalize to `interview`. An
   unrecognized string (e.g. `"onboarding"`) normalizes to `null` with
   `valid=false`; `validate-transition` treats an unknown side as a structured
   `unknown_from_status` / `unknown_to_status` error rather than a crash.

## Why these rules

- **Monotonic forward** mirrors how recruiting actually works and keeps the
  event log meaningful (each event is a real-world stage change).
- **`withdrawn` from anywhere** avoids dead-ends: a user who loses interest at
  `screening` should not have to fabricate an `interview` event to exit.
- **Terminal states are sinks** so that a finished application (hired, rejected,
  or abandoned) cannot be silently resurrected - the user starts a fresh
  `saved` record for a new application instead.
