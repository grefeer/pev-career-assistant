#!/bin/bash
# MySQL daily backup with gzip compression and 7-day retention
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/career-assistant/mysql}"
RETENTION_DAYS=7
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/mysql_$TIMESTAMP.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Starting MySQL backup..."

mysqldump \
    --host=127.0.0.1 --port=3306 \
    --user="${BACKUP_USER:-backup}" \
    --password="${BACKUP_PASSWORD:-}" \
    --single-transaction \
    --routines --triggers --events \
    career_assistant | gzip > "$BACKUP_FILE"

echo "[$(date)] Backup complete: $BACKUP_FILE"

# Remove backups older than RETENTION_DAYS
find "$BACKUP_DIR" -name "mysql_*.sql.gz" -mtime +$RETENTION_DAYS -delete

echo "[$(date)] Cleaned up old backups (retention: ${RETENTION_DAYS}d)"
