#!/bin/bash
# MinIO object sync to backup location (7-day retention)
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/career-assistant/minio}"
TIMESTAMP=$(date +%Y%m%d)
RETENTION_DAYS=7

mkdir -p "$BACKUP_DIR/$TIMESTAMP"

echo "[$(date)] Starting MinIO sync..."

# Using mc (MinIO Client) to mirror objects
mc mirror --preserve myminio/career-assistant "$BACKUP_DIR/$TIMESTAMP/"

echo "[$(date)] Sync complete: $BACKUP_DIR/$TIMESTAMP"

# Remove directories older than RETENTION_DAYS
find "$BACKUP_DIR" -maxdepth 1 -type d -mtime +$RETENTION_DAYS -exec rm -rf {} \;

echo "[$(date)] Cleaned up old syncs (retention: ${RETENTION_DAYS}d)"
