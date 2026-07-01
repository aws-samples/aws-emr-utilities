#!/bin/bash
# switch-sft.sh
# Switches the store file tracker from DEFAULT to FILE for all specified tables
# and validates data accessibility after each switch.
# Run this on the Read Replica cluster after generate-filelists.sh (Step 8 in the migration guide).
#
# Usage: ./switch-sft.sh [table1 table2 ...]
#   If no tables specified, discovers all user tables automatically.

set -euo pipefail

LOG_DIR="/tmp/sft-migration-logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/switch-sft_${TIMESTAMP}.log"

echo "=== change_sft to FILE - $(date) ===" | tee "$LOG_FILE"

# Get table list
if [ $# -gt 0 ]; then
  TABLES=("$@")
else
  echo "Discovering user tables..." | tee -a "$LOG_FILE"
  TABLES=($(echo "list" | hbase shell 2>/dev/null | grep -v "^TABLE$" | grep -v "^hbase:" | grep -v "row(s)" | grep -v "^$" | grep -v "=>"))
fi

echo "Tables to process: ${TABLES[*]}" | tee -a "$LOG_FILE"
echo "---" | tee -a "$LOG_FILE"

FAILED=0
SUCCEEDED=0

for TABLE in "${TABLES[@]}"; do
  echo "[$(date +%H:%M:%S)] Switching SFT for: $TABLE" | tee -a "$LOG_FILE"
  
  # Switch the store file tracker
  RESULT=$(echo "change_sft '$TABLE', 'FILE'" | hbase shell 2>&1)
  echo "$RESULT" | tee -a "$LOG_FILE"
  
  # Validate with a scan
  echo "[$(date +%H:%M:%S)] Validating scan on: $TABLE" | tee -a "$LOG_FILE"
  SCAN_RESULT=$(echo "scan '$TABLE', {LIMIT => 1}" | hbase shell 2>&1)
  
  if echo "$SCAN_RESULT" | grep -q "row(s)"; then
    ((SUCCEEDED++))
    echo "[$(date +%H:%M:%S)] ✓ $TABLE switched and validated" | tee -a "$LOG_FILE"
  else
    ((FAILED++))
    echo "[$(date +%H:%M:%S)] ✗ $TABLE - scan validation FAILED" | tee -a "$LOG_FILE"
    echo "$SCAN_RESULT" | tee -a "$LOG_FILE"
  fi
  echo "---" | tee -a "$LOG_FILE"
done

echo "" | tee -a "$LOG_FILE"
echo "=== Summary ===" | tee -a "$LOG_FILE"
echo "Succeeded: $SUCCEEDED" | tee -a "$LOG_FILE"
echo "Failed:    $FAILED" | tee -a "$LOG_FILE"
echo "Log file:  $LOG_FILE" | tee -a "$LOG_FILE"

if [ $FAILED -gt 0 ]; then
  echo "WARNING: Some tables failed validation. DO NOT proceed with cutover." | tee -a "$LOG_FILE"
  exit 1
fi

echo "" | tee -a "$LOG_FILE"
echo "All tables switched to FILE tracker successfully. Safe to proceed with Phase 2 (Cutover)." | tee -a "$LOG_FILE"
