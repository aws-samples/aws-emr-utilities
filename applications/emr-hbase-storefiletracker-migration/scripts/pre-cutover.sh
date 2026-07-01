#!/bin/bash
# pre-cutover.sh
# Prepares the primary cluster for cutover by disabling balancing/compactions and taking snapshots.
# Run this on the PRIMARY cluster (Step 9 in the migration guide).
#
# Usage: ./pre-cutover.sh [table1 table2 ...]
#   If no tables specified, discovers all user tables automatically.

set -euo pipefail

LOG_DIR="/tmp/sft-migration-logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/pre-cutover_${TIMESTAMP}.log"

echo "=== Pre-Cutover Preparation - $(date) ===" | tee "$LOG_FILE"

# Get table list
if [ $# -gt 0 ]; then
  TABLES=("$@")
else
  echo "Discovering user tables..." | tee -a "$LOG_FILE"
  TABLES=($(echo "list" | hbase shell 2>/dev/null | grep -v "^TABLE$" | grep -v "^hbase:" | grep -v "row(s)" | grep -v "^$" | grep -v "=>"))
fi

echo "Tables: ${TABLES[*]}" | tee -a "$LOG_FILE"
echo "---" | tee -a "$LOG_FILE"

# Disable balancer
echo "[$(date +%H:%M:%S)] Disabling balancer..." | tee -a "$LOG_FILE"
echo "balance_switch false" | hbase shell 2>&1 | tee -a "$LOG_FILE"

# Disable compactions
echo "[$(date +%H:%M:%S)] Disabling compactions..." | tee -a "$LOG_FILE"
echo "compaction_switch false" | hbase shell 2>&1 | tee -a "$LOG_FILE"

echo "---" | tee -a "$LOG_FILE"

# Take snapshots
DATE_SUFFIX=$(date +%Y%m%d)
for TABLE in "${TABLES[@]}"; do
  SNAPSHOT_NAME="${TABLE}_pre_migration_${DATE_SUFFIX}"
  echo "[$(date +%H:%M:%S)] Creating snapshot: $SNAPSHOT_NAME" | tee -a "$LOG_FILE"
  echo "snapshot '$TABLE', '$SNAPSHOT_NAME'" | hbase shell 2>&1 | tee -a "$LOG_FILE"
done

echo "---" | tee -a "$LOG_FILE"

# List snapshots for confirmation
echo "[$(date +%H:%M:%S)] Listing all snapshots:" | tee -a "$LOG_FILE"
echo "list_snapshots" | hbase shell 2>&1 | tee -a "$LOG_FILE"

echo "" | tee -a "$LOG_FILE"
echo "=== Pre-cutover complete ===" | tee -a "$LOG_FILE"
echo "Balancer: DISABLED" | tee -a "$LOG_FILE"
echo "Compactions: DISABLED" | tee -a "$LOG_FILE"
echo "Snapshots: Created for ${#TABLES[@]} table(s)" | tee -a "$LOG_FILE"
echo "Log file: $LOG_FILE" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"
echo "Next: Run final refresh on the Read Replica (Step 10), then proceed to Phase 2 Cutover." | tee -a "$LOG_FILE"
