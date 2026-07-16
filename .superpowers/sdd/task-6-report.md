# Task 6 Report: Windows device pairing, authentication, heartbeat, and revoke

## Status

Implemented the Task 6 device workflow only.

## Delivered

- One-time pairing tickets stored at `pairing-ticket:{sha256(code)}` as JSON containing only `user_id` and `created_at`, with a 600-second TTL and atomic Redis `GETDEL` redemption.
- Windows device issuance with `secrets.token_urlsafe(32)`; only the SHA-256 digest is stored in MySQL and authentication uses `hmac.compare_digest`.
- MySQL-authoritative active/revoked device state, owner-scoped listing and revocation, and immediate rejection of revoked tokens.
- Heartbeat persistence of `last_seen_at` and `version`, plus `device-online:{device_id}=1` with a 90-second Redis TTL.
- Device online display derived only from Redis; losing online keys reports offline without changing MySQL device status.
- Audit events for pairing-ticket creation, successful pairing, and revocation. Audit payload keys are restricted to `platform`, `version`, and `result` and do not include pairing codes, tokens, or public keys.
- All six `/api/devices` endpoints with fixed 400/401/404 behavior and response models that do not expose `token_hash` or `public_key_pem`.
- Application lifespan Redis client creation/closure. `REDIS_PASSWORD` is read only from the environment and is never logged or returned.

## TDD evidence

1. Initial unit RED: `python -m pytest tests/unit/test_device_service.py -v` failed during collection with `ModuleNotFoundError: No module named 'backend.app.services.devices'`.
2. Exact service-interface RED: `create_pairing_ticket(user_id=...)` failed with the expected missing-`db` `TypeError`, then passed after adding the compatible optional audit-session argument.
3. Device unit/contract GREEN: 11 tests passed, exercising fakeredis `GETDEL`, bytes JSON, pairing TTL (599-600 after Redis second rounding), online TTL (1-90), replay, revoke, heartbeat, redaction, credential response boundaries, and ownership isolation.

## Final verification

- `git diff --check`: passed.
- `.venv\\Scripts\\python.exe -m compileall -q backend\\app`: passed.
- `.venv\\Scripts\\python.exe -m pytest -v`: **244 passed, 2 skipped** in 33.05 seconds.
- The two skips are pre-existing opt-in MySQL integration tests; no development Redis connection was used by the new tests.

## Notes / concerns

- Unit and contract tests use `fakeredis`; no real `redis-custom` integration was necessary for this task.
- Ticket creation requires a DB session so no unaudited service-level bypass exists.

## Review fix (2026-07-14)

The review findings were addressed in a separate fix:

- Removed the optional-DB ticket creation path. Every ticket creation now requires a database session and a successful `AuditEvent` commit.
- If ticket audit flush/commit fails, the SQLAlchemy transaction is rolled back and the newly written Redis ticket key is deleted; no usable unaudited ticket is returned.
- Redemption now validates `created_at` from the consumed JSON and pre-generates the device id and token digest before persistence.
- If device/audit persistence fails after `GETDEL`, the service rolls back and queries authoritative MySQL by both device id and token digest:
  - If both identify the same committed device (for example, commit succeeded but its acknowledgement was lost), the original issued device/token is returned and the ticket is not restored.
  - If neither exists, the original raw ticket JSON is restored only with Redis `SET NX` and only for the integer seconds remaining in its original 600-second window.
  - If rollback/query cannot determine the result, or id/digest results conflict, `PairingPersistenceUncertainError` is raised and the ticket is not restored, preferring no duplicate credential issuance.
- Malformed ticket JSON is consumed and is never restored.
- Added explicit Pydantic response models and route `response_model` declarations for pairing tickets, device summary, pair response, list response, and heartbeat response. OpenAPI confirms only the pair response exposes `device_token`; list and `/me` expose no token hash, public key, or device token.

### Review TDD evidence

- RED: ticket audit insert failure left a `pairing-ticket:*` key behind.
- RED: paired audit flush failure and explicit pre-commit failure permanently consumed the code (`TTL == -2`).
- RED: OpenAPI contained no named `PairingTicketResponse`/device response schemas.
- GREEN: audit failure cleanup, flush compensation, explicit commit compensation, original-TTL preservation, NX race protection, malformed-ticket non-restoration, commit-ACK-loss recovery, and OpenAPI schema tests all pass.

### Review final verification

- Device unit/contract suite: **17 passed**.
- Full suite: **250 passed, 2 skipped** in 47.95 seconds.
- `ruff check` on all Task 6 production/test files: passed.
- `mypy --explicit-package-bases --follow-imports=skip` on the Task 6 production files: passed with no issues. The flags are required by this repository's namespace-package layout and avoid unrelated pre-existing transitive modules.
- `compileall`, `git diff --check`: passed.
