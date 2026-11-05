#!/bin/bash
set -eo pipefail

CONFIG_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$CONFIG_DIR/data_backups}"

SKIP_EXISTING=false
ARCHIVE=""

# Parse arguments
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-existing|-k|--keep-newer)
      SKIP_EXISTING=true
      shift
      ;;
    *)
      ARCHIVE="$1"
      shift
      ;;
  esac
done

if [ -z "$ARCHIVE" ]; then
  ARCHIVE="$BACKUP_DIR/latest.tar.gz"
fi

if [ ! -f "$ARCHIVE" ]; then
  echo "Error: Backup archive not found at: $ARCHIVE"
  echo "Usage: $0 [--keep-existing] [path/to/archive.tar.gz]"
  exit 1
fi

echo "=== Starting Data Restore ==="
echo "Config directory: $CONFIG_DIR"
echo "Source archive:   $ARCHIVE"
echo "Skip existing:    $SKIP_EXISTING"

cd "$CONFIG_DIR"

if [ "$SKIP_EXISTING" = true ]; then
  # -k: keep existing files, do not overwrite
  tar -xzkf "$ARCHIVE" -C "$CONFIG_DIR" 2>/dev/null || true
  echo "Restored missing files from archive (existing files kept intact)."
else
  tar -xzf "$ARCHIVE" -C "$CONFIG_DIR"
  echo "All files restored from archive."
fi

echo "=== Restore completed successfully! ==="
