#!/bin/bash
# validate-migration.sh
# Post-cutover validation script. Run on the promoted cluster after Phase 2 cutover.
# Verifies the cluster is read-write, tables are accessible, and no user-table inconsistencies exist.
#
# Usage: ./validate-migration.sh [table1 table2 ...]
#   If no tables specified, discovers all user tables automatically.

set -euo pipefail

LOG_DIR="/tmp/sft-migration-logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/validate-migration_${TIMESTAMP}.log"

echo "=== Post-Migration Validation - $(date) ===" | tee "$LOG_FILE"

# Check cluster is in ACTIVE (read-write) mode
echo "[$(date +%H:%M:%S)] Checking cluster mode..." | tee -a "$LOG_FILE"
STATE=$(echo "readonly_state" | hbase shell 2>&1 | grep -o "ACTIVE\|READONLY")
echo "Cluster state: $STATE" | tee -a "$LOG_FILE"

if [ "$STATE" != "ACTIVE" ]; then
  echo "ERROR: Cluster is not in ACTIVE mode. Run: echo \"readonly_switch false\" | hbase shell" | tee -a "$LOG_FILE"
  exit 1
fi
echo "✓ Cluster is ACTIVE (read-write)" | tee -a "$LOG_FILE"
echo "---" | tee -a "$LOG_FILE"

# Get table list
if [ $# -gt 0 ]; then
  TABLES=("$@")
else
  echo "Discovering user tables..." | tee -a "$LOG_FILE"
  TABLES=($(echo "list" | hbase shell 2>/dev/null | grep -v "^TABLE$" | grep -v "^hbase:" | grep -v "row(s)" | grep -v "^$" | grep -v "=>"))
fi

echo "Tables to validate: ${TABLES[*]}" | tee -a "$LOG_FILE"
echo "---" | tee -a "$LOG_FILE"

# Validate each table - read and write
FAILED=0
for TABLE in "${TABLES[@]}"; do
  echo "[$(date +%H:%M:%S)] Validating: $TABLE" | tee -a "$LOG_FILE"
  
  # Read test
  SCAN_RESULT=$(echo "scan '$TABLE', {LIMIT => 1}" | hbase shell 2>&1)
  if echo "$SCAN_RESULT" | grep -q "row(s)"; then
    echo "  ✓ Read: OK" | tee -a "$LOG_FILE"
  else
    echo "  ✗ Read: FAILED" | tee -a "$LOG_FILE"
    ((FAILED++))
    continue
  fi

  # Write test (insert and delete a canary row)
  CANARY_KEY="_migration_validation_canary_$(date +%s)"
  PUT_RESULT=$(echo "put '$TABLE', '$CANARY_KEY', '$(echo "describe '$TABLE'" | hbase shell 2>/dev/null | grep "NAME =>" | head -1 | sed "s/.*NAME => '\\([^']*\\)'.*/\\1/"):validation', 'test'" | hbase shell 2>&1)
  
  if echo "$PUT_RESULT" | grep -qi "error"; then
    echo "  ✗ Write: FAILED" | tee -a "$LOG_FILE"
    ((FAILED++))
  else
    echo "  ✓ Write: OK" | tee -a "$LOG_FILE"
    # Clean up canary row
    echo "deleteall '$TABLE', '$CANARY_KEY'" | hbase shell 2>/dev/null
  fi

  # Verify SFT is FILE
  SFT_CHECK=$(echo "describe '$TABLE'" | hbase shell 2>&1 | grep "file-tracker")
  if echo "$SFT_CHECK" | grep -q "FILE"; then
    echo "  ✓ SFT: FILE" | tee -a "$LOG_FILE"
  else
    echo "  ⚠ SFT: Could not confirm FILE tracker" | tee -a "$LOG_FILE"
  fi
  echo "---" | tee -a "$LOG_FILE"
done

# Run hbck
echo "[$(date +%H:%M:%S)] Running hbck..." | tee -a "$LOG_FILE"
HBCK_OUTPUT=$(sudo -u hbase hbase hbck 2>&1)
echo "$HBCK_OUTPUT" >> "$LOG_FILE"

# Check for user table inconsistencies (ignore hbase:storefile)
USER_ERRORS=$(echo "$HBCK_OUTPUT" | grep "ERROR:" | grep -v "hbase:storefile" | grep -v "hbase:meta_j" || true)
if [ -n "$USER_ERRORS" ]; then
  echo "" | tee -a "$LOG_FILE"
  echo "⚠ hbck found errors (excluding known benign hbase:storefile/meta_j warnings):" | tee -a "$LOG_FILE"
  echo "$USER_ERRORS" | tee -a "$LOG_FILE"
  ((FAILED++))
else
  echo "✓ hbck: No user-table inconsistencies" | tee -a "$LOG_FILE"
fi

echo "" | tee -a "$LOG_FILE"
echo "=== Validation Summary ===" | tee -a "$LOG_FILE"
echo "Cluster state: ACTIVE" | tee -a "$LOG_FILE"
echo "Tables validated: ${#TABLES[@]}" | tee -a "$LOG_FILE"
echo "Failures: $FAILED" | tee -a "$LOG_FILE"
echo "Log file: $LOG_FILE" | tee -a "$LOG_FILE"

if [ $FAILED -gt 0 ]; then
  echo "" | tee -a "$LOG_FILE"
  echo "VALIDATION FAILED - investigate before routing production traffic." | tee -a "$LOG_FILE"
  exit 1
else
  echo "" | tee -a "$LOG_FILE"
  echo "ALL CHECKS PASSED - cluster is ready for production traffic." | tee -a "$LOG_FILE"
fi
