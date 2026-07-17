# Windows Executor Simulation Runbook

This runbook covers setup, running, and troubleshooting the Windows Executor
simulation on a local development machine.

---

## 1. Prerequisites

- Python 3.12+
- Google Chrome or Chromium installed
- Git checkout of the project on a Windows machine

---

## 2. Virtual Environment and Dependencies

Create and activate a venv at the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install executor dependencies:

```powershell
pip install -r requirements-executor.txt
pip install playwright
playwright install chromium
```

Install backend and test dependencies:

```powershell
pip install -r requirements.txt -r requirements-dev.txt
```

---

## 3. Mock Recruitment Site

The mock site is a FastAPI app at `executor/mock_site/app.py`. Start it on a
loopback port:

```powershell
python -c "
import uvicorn
from executor.mock_site.app import app
uvicorn.run(app, host='127.0.0.1', port=8765, log_level='info')
"
```

The mock site serves these endpoints:

| Route | Description |
|---|---|
| `/single-page` | Single-page form (bottom action) |
| `/multi-step/1` | Multi-step step 1 (safe intermediate) |
| `/multi-step/2` | Multi-step step 2 (final action) |
| `/ambiguous` | Ambiguous action page |
| `/human-gate` | Login / captcha gate |
| `/readback-mismatch` | Field that resets after fill |
| `/submission-success` | Result: success |
| `/submission-failed` | Result: failed |
| `/submission-unknown` | Result: unknown |
| `/event` | POST telemetry event |
| `/telemetry` | GET telemetry snapshot |
| `/reset` | POST reset telemetry |

Telemetry tracks field fills, intermediate clicks, final clicks, and ambiguous
clicks. Reset between tests via `POST /reset`.

---

## 4. Backend Setup (for API-bound simulation)

When the simulation contacts a real backend (via `base-url`), start the backend
with:

```powershell
# Ensure database is migrated
alembic upgrade head

# Start the API server
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

The backend provides these executor-facing routes:

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/api/devices/pair` | Pair a device (code + key) |
| `POST` | `/api/devices/heartbeat` | Keep-alive |
| `POST` | `/api/devices/task-lease` | Issue a task lease |
| `GET` | `/api/executor/tasks` | List assigned tasks |
| `GET` | `/api/executor/tasks/{id}` | Get task detail (+ payload) |
| `POST` | `/api/executor/tasks/{id}/progress` | Report fill progress |
| `POST` | `/api/executor/tasks/{id}/result` | Report observation result |

---

## 5. Pairing a Device

The CLI `pair` command generates an RSA-3072 key pair, sends the public key to
the backend with a pairing code, and stores the device token in the Windows
Credential Locker (via `keyring`).

```powershell
# Create a pairing ticket via the backend API (as an authenticated user):
# POST /api/devices/pairing-tickets

# Then pair from the CLI:
python -m executor.cli pair --base-url http://127.0.0.1:8000 --device-name "My Windows PC"
```

The CLI prompts for the pairing code via `getpass`. After success, the device
token is stored in the credential locker under the service name
`career-assistant-executor`.

Keyring delegates to Windows Credential Manager, so credentials survive reboots.

---

## 6. Seeding a Simulation Task

Create a task in the database for simulation:

```powershell
# Using the backend API as an authenticated user:
# POST /api/admin/jobs/completion  (to create a reviewed job posting)
# Then the system creates an ApplicationTask in DISPATCHED status.

# Or, for testing, insert a task directly:
python -c "
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from backend.app.db.base import Base
from backend.app.db.models import ApplicationTask, ApplicationTaskStatus

engine = create_engine('postgresql://user:pass@localhost/dbname')
Base.metadata.create_all(engine)
with Session(engine) as db:
    task = ApplicationTask(
        user_id='<user-id>',
        target_job_id='simulation-job',
        device_id='<device-id>',
        status=ApplicationTaskStatus.DISPATCHED,
    )
    db.add(task)
    db.commit()
    print(f'Task created: {task.id}')
"
```

The `target_job_id` determines which mock-site route is used when
`SimulationExecutorPayloadProvider` is active:

| `target_job_id` | Mock route |
|---|---|
| `simulation-single` | `/single-page` |
| `simulation-multi` | `/multi-step/1` |
| `simulation-ambiguous` | `/ambiguous` |
| `simulation-human` | `/human-gate` |
| `simulation-mismatch` | `/readback-mismatch` |
| `simulation-result-success` | `/submission-success` |
| `simulation-result-failed` | `/submission-failed` |
| `simulation-result-unknown` | `/submission-unknown` |

---

## 7. Running a Simulation

Load a fixture JSON file matching `ExecutorTaskPayload` and run the simulation:

```powershell
python -m executor.cli run-simulation ^
    --base-url http://127.0.0.1:8000 ^
    --fixture tests/fixtures/executor/protocol_v1/task.json ^
    --data-dir ./sim-data
```

The fixture URL must point to a loopback address (`127.0.0.1` or `localhost`).
The CLI rejects non-loopback URLs with a fatal error.

Checkpoints are written to `./sim-data/checkpoints/<task_id>.json`. The browser
profile is stored at `./sim-data/chrome-profile/`.

---

## 8. Resuming a Simulation

If a checkpoint exists from a previous run, resume with:

```powershell
python -m executor.cli resume-simulation ^
    --base-url http://127.0.0.1:8000 ^
    --fixture tests/fixtures/executor/protocol_v1/task.json ^
    --data-dir ./sim-data
```

The CLI checks that a checkpoint file exists for the task before proceeding.
If no checkpoint is found, it exits with a non-zero status and an error message.

The engine detects checkpoint fields and skips already-completed fields on
recovery.

---

## 9. Checkpoint Locations

Checkpoints are JSON files stored at `<data-dir>/checkpoints/<task_id>.json`.

A checkpoint contains:

```json
{
  "protocol_version": "executor.v1",
  "task_id": "...",
  "task_state_version": 1,
  "step": "fill_page",
  "page_index": 1,
  "page_fingerprint": "sha256:abc123...",
  "completed_field_keys": ["full_name"],
  "completed_effect_keys": ["page-1:safe-next"],
  "pending_field_key": null,
  "pending_effect_key": null,
  "issue_counts": {
    "missing": 0,
    "low": 0,
    "readback": 0,
    "defaulted": 0
  }
}
```

Checkpoints never contain form field values, credentials, tokens, or other
sensitive data.

---

## 10. Lease and 409 Conflict Recovery

When a task lease expires or the backend returns a 409 Conflict, the engine
stops and reports the outcome rather than retrying blindly.

Common conflict error codes:

| Code | Meaning |
|---|---|
| `stale_task_version` | Task state changed since lease was issued |
| `invalid_executor_transition` | Task status does not allow the requested transition |
| `executor_payload_unavailable` | Payload provider has no data for this task |

On 409, the engine enters `stopped_conflict` outcome. No browser actions are
retried after a conflict.

---

## 11. Device Revocation

To revoke a device:

```powershell
# As the user who owns the device:
curl -X DELETE http://127.0.0.1:8000/api/devices/<device-id> \
    -H "Authorization: Bearer <user-token>"
```

After revocation:
- The device token is invalidated
- The backend returns 401 on any executor API call
- Future heartbeat and lease requests fail
- The device status changes to `REVOKED`

---

## 12. Telemetry Assertions

The mock site maintains in-memory telemetry counters. After a simulation run,
inspect them:

```powershell
curl http://127.0.0.1:8765/telemetry
```

Expected telemetry for a successful single-page simulation:

```json
{
  "field_events": {"full_name": 1},
  "intermediate_clicks": 0,
  "final_clicks": 0,
  "ambiguous_clicks": 0
}
```

For a multi-step simulation with a safe intermediate click:

```json
{
  "field_events": {"full_name": 1},
  "intermediate_clicks": 1,
  "final_clicks": 0,
  "ambiguous_clicks": 0
}
```

The safety gate guarantees that final actions and ambiguous actions are never
clicked. Verify by checking that `final_clicks` and `ambiguous_clicks` remain
at 0.

---

## 13. Troubleshooting

**Issue: `playwright` not found**
```powershell
pip install playwright
playwright install chromium
```

**Issue: `keyring` fails to store credentials on Windows**
Ensure the Windows Credential Manager service is running. Keyring delegates to
the Windows Credential Locker, which requires the service to be active.

**Issue: Simulation rejects non-loopback URL**
The CLI enforces loopback-only for simulation. Use `127.0.0.1` or `localhost`.

**Issue: Checkpoint not found on resume**
Ensure `--data-dir` points to the same directory used for the original
`run-simulation` command. The checkpoint file must exist at
`<data-dir>/checkpoints/<task-id>.json`.

**Issue: 401 Unauthorized from backend**
The device token may be missing from the credential store. Re-run `pair`.

**Issue: 409 Conflict during simulation**
The task was updated by another actor (e.g., a human reviewer cancelled it).
Check the task status via the backend API.
