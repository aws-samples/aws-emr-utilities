#!/bin/bash
# generate-filelists.sh
# Generates .filelist manifests for all user tables using the HBCK2 generateStoreFileList tool.
# Run this on the Read Replica cluster after refresh_meta (Step 7 in the migration guide).
#
# Usage: ./generate-filelists.sh [table1 table2 ...]
#   If no tables specified, discovers all user tables automatically.

set -euo pipefail

HBCK2_JAR="/usr/lib/hbase-operator-tools/hbase-hbck2-1.2.0.jar"
LOG_DIR="/tmp/sft-migration-logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/generate-filelists_${TIMESTAMP}.log"

echo "=== generateStoreFileList - $(date) ===" | tee "$LOG_FILE"

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
  echo "[$(date +%H:%M:%S)] Processing: $TABLE" | tee -a "$LOG_FILE"
  
  if sudo -u hbase hbase hbck -j "$HBCK2_JAR" generateStoreFileList "$TABLE" 2>&1 | tee -a "$LOG_FILE"; then
    ((SUCCEEDED++))
    echo "[$(date +%H:%M:%S)] ✓ $TABLE completed" | tee -a "$LOG_FILE"
  else
    ((FAILED++))
    echo "[$(date +%H:%M:%S)] ✗ $TABLE FAILED" | tee -a "$LOG_FILE"
  fi
  echo "---" | tee -a "$LOG_FILE"
done

echo "" | tee -a "$LOG_FILE"
echo "=== Summary ===" | tee -a "$LOG_FILE"
echo "Succeeded: $SUCCEEDED" | tee -a "$LOG_FILE"
echo "Failed:    $FAILED" | tee -a "$LOG_FILE"
echo "Log file:  $LOG_FILE" | tee -a "$LOG_FILE"

if [ $FAILED -gt 0 ]; then
  echo "WARNING: Some tables failed. Check the log for details." | tee -a "$LOG_FILE"
  exit 1
fi
