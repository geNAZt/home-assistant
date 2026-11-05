#!/bin/bash
set -eo pipefail

CONFIG_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$CONFIG_DIR/data_backups}"
mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date +"%Y%m%d_%H%M%S")"
ARCHIVE="$BACKUP_DIR/data_backup_${TIMESTAMP}.tar.gz"
LATEST="$BACKUP_DIR/latest.tar.gz"

echo "=== Starting Data Backup ==="
echo "Config directory:   $CONFIG_DIR"
echo "Backup destination: $ARCHIVE"

TMP_FILE_LIST="$(mktemp /tmp/ha_backup_files.XXXXXX)"
trap 'rm -f "$TMP_FILE_LIST"' EXIT

cd "$CONFIG_DIR"

# Collect all .db* and .json files outside custom_components, plus logs and washdata gz assets
find . -not -path '*/.*' -not -path './custom_components*' -type f \( -name "*.db*" -o -name "*.json" -o -name "*.log" \) \
  | sed 's|^\./||' > "$TMP_FILE_LIST"

if [ -d "custom_components/ha_washdata/www" ]; then
  find custom_components/ha_washdata/www -type f -name "*.gz" | sed 's|^\./||' >> "$TMP_FILE_LIST" || true
fi

if [ ! -s "$TMP_FILE_LIST" ]; then
  echo "No matching data files found to back up."
  exit 0
fi

FILE_COUNT=$(wc -l < "$TMP_FILE_LIST" | tr -d ' ')
echo "Found $FILE_COUNT files to back up:"
sed 's/^/  - /' "$TMP_FILE_LIST"

# Create archive
tar -czf "$ARCHIVE" -T "$TMP_FILE_LIST"
cp -f "$ARCHIVE" "$LATEST"

ARCHIVE_SIZE=$(ls -lh "$ARCHIVE" | awk '{print $5}')
echo "=== Backup completed successfully! ==="
echo "Archive: $ARCHIVE ($ARCHIVE_SIZE)"
echo "Latest:  $LATEST"

# Keep last 10 backups
ls -1t "$BACKUP_DIR"/data_backup_*.tar.gz 2>/dev/null | tail -n +11 | xargs -r rm -f 2>/dev/null || true
