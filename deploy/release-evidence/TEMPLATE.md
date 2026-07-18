# Release Evidence — {date}

## Code

| Item | Value |
|------|-------|
| Git SHA | |
| Branch | |
| Working tree clean | Yes / No |

## Database

| Item | Value |
|------|-------|
| Alembic head | |
| head->base->head roundtrip | Pass / Fail |

## Images

| Image | Digest |
|-------|--------|
| career-assistant-backend | |
| career-assistant-frontend | |
| prom/prometheus | |
| grafana/grafana | |

## Backend Gates

| Gate | Result |
|------|--------|
| Ruff | Pass / Fail (N violations) |
| pytest unit/contract/security | N passed, M skipped |
| pytest integration (MySQL) | N passed |
| pytest integration (MinIO) | N passed |
| pytest integration (Nginx) | N passed |
| Tencent live opt-in | N passed |

## Frontend Gates

| Gate | Result |
|------|--------|
| npm ci | Pass / Fail |
| vitest | N passed |
| vue-tsc | Pass / Fail (N errors) |
| vite build | Pass / Fail |
| npm audit --audit-level=high | 0 vulnerabilities / N issues |

## Ops

| Check | Result |
|-------|--------|
| Backup success (MySQL) | Yes / No |
| Backup success (MinIO) | Yes / No |
| Restore drill | Pass / Fail |
| Health check | All up / Issues |
| Prometheus scrape | OK / Fail |

## Grayscale Batch

| Field | Value |
|-------|-------|
| Batch number | |
| Site | |
| User count | |
| Task count | |
| Auto-submit events | 0 / N |
| Rollback triggered | Yes / No |
