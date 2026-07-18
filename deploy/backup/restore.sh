#!/bin/bash
# Restore MySQL from backup
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <backup_file.sql.gz> [target_database]"
    echo "  target_database defaults to career_assistant_restore_test"
    exit 1
fi

BACKUP_FILE="$1"
TARGET_DB="${2:-career_assistant_restore_test}"

if [ ! -f "$BACKUP_FILE" ]; then
    echo "Backup file not found: $BACKUP_FILE"
    exit 1
fi

echo "[$(date)] Creating restore database: $TARGET_DB"
mysql -u root -e "CREATE DATABASE IF NOT EXISTS $TARGET_DB;"

echo "[$(date)] Restoring from: $BACKUP_FILE"
gunzip < "$BACKUP_FILE" | mysql "$TARGET_DB"

echo "[$(date)] Verifying table count..."
TABLE_COUNT=$(mysql -u root -e \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$TARGET_DB';" -N)
echo "Restored $TABLE_COUNT tables to $TARGET_DB"

echo "[$(date)] Restore complete."
