#!/bin/bash
##############################################################################
# Check Iceberg Table Compaction Status
##############################################################################
# Displays current statistics for all Iceberg tables including:
# - Row counts
# - File counts and sizes
# - Snapshot counts
# - Last compaction time
#
# Usage:
#   ./34_check_compaction_status.sh
#   ./34_check_compaction_status.sh --table backlog_events_log_v5
#   ./34_check_compaction_status.sh --detailed
##############################################################################

set -e

# Configuration
AWS_REGION="${AWS_REGION:-us-east-1}"
S3_BUCKET="${S3_BUCKET:-${S3_BUCKET}}"

# Tables to check
TABLES=(
    "${CATALOG_NAMESPACE}.backlog_events_log_v5"
    "${CATALOG_NAMESPACE}.spark_metrics_task_stage_v5"
    "${CATALOG_NAMESPACE}.spark_metrics_config_v5"
    "${CATALOG_NAMESPACE}.serverless_config_advisor_v5"
)

# Parse arguments
SPECIFIC_TABLE=""
DETAILED=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --table)
            SPECIFIC_TABLE="${CATALOG_NAMESPACE}.$2"
            shift 2
            ;;
        --detailed)
            DETAILED=true
            shift
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --table TABLE_NAME    Check specific table only"
            echo "  --detailed            Show detailed statistics"
            echo "  --help                Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Filter tables if specific table requested
if [ -n "$SPECIFIC_TABLE" ]; then
    TABLES=("$SPECIFIC_TABLE")
fi

echo "=============================================================================="
echo "ICEBERG TABLE COMPACTION STATUS"
echo "=============================================================================="
echo "Timestamp: $(date '+%Y-%m-%d %H:%M:%S')"
echo "Region:    $AWS_REGION"
echo "=============================================================================="

for table in "${TABLES[@]}"; do
    echo ""
    echo "Table: $table"
    echo "----------------------------------------------------------------------"

    # Check if table exists
    if ! spark-sql -e "DESCRIBE TABLE $table" &>/dev/null; then
        echo "  ⚠ Table not found or not accessible"
        continue
    fi

    # Get row count
    echo -n "  Checking row count... "
    row_count=$(spark-sql -e "SELECT COUNT(*) as cnt FROM $table" 2>/dev/null | tail -n 1 || echo "0")
    echo "$row_count rows"

    # Get file statistics
    echo -n "  Checking file statistics... "
    file_stats=$(spark-sql -e "
        SELECT
            COUNT(*) as file_count,
            ROUND(AVG(file_size_in_bytes)/(1024*1024), 2) as avg_size_mb,
            ROUND(SUM(file_size_in_bytes)/(1024*1024*1024), 2) as total_size_gb
        FROM $table.files
    " 2>/dev/null | tail -n 1 || echo "0,0,0")

    file_count=$(echo "$file_stats" | awk -F'\t' '{print $1}')
    avg_size_mb=$(echo "$file_stats" | awk -F'\t' '{print $2}')
    total_size_gb=$(echo "$file_stats" | awk -F'\t' '{print $3}')

    echo "$file_count files"
    echo "    Avg file size: ${avg_size_mb} MB"
    echo "    Total size:    ${total_size_gb} GB"

    # Get snapshot count
    echo -n "  Checking snapshots... "
    snapshot_count=$(spark-sql -e "SELECT COUNT(*) as cnt FROM $table.snapshots" 2>/dev/null | tail -n 1 || echo "0")
    echo "$snapshot_count snapshots"

    # Get last compaction info
    if [ "$DETAILED" = true ]; then
        echo "  Recent snapshots:"
        spark-sql -e "
            SELECT
                snapshot_id,
                operation,
                FROM_UNIXTIME(committed_at/1000, 'yyyy-MM-dd HH:mm:ss') as committed_at
            FROM $table.snapshots
            ORDER BY committed_at DESC
            LIMIT 5
        " 2>/dev/null | while IFS= read -r line; do
            echo "    $line"
        done
    fi

    # Health check
    echo -n "  Health: "
    if [ "$file_count" -eq 0 ]; then
        echo "❌ EMPTY (no data files)"
    elif [ "$snapshot_count" -eq 0 ]; then
        echo "❌ CRITICAL (no snapshots)"
    elif [ "$snapshot_count" -gt 20 ]; then
        echo "⚠️  WARNING (too many snapshots: $snapshot_count > 20)"
    elif [ $(echo "$avg_size_mb < 50" | bc -l) -eq 1 ]; then
        echo "⚠️  WARNING (small files: avg ${avg_size_mb}MB < 50MB)"
    else
        echo "✓ GOOD"
    fi

    echo "----------------------------------------------------------------------"
done

echo ""
echo "=============================================================================="
echo "SUMMARY"
echo "=============================================================================="

# Overall health summary
echo "Compaction Recommendations:"
echo ""

# Check each table and provide recommendations
for table in "${TABLES[@]}"; do
    table_name=$(echo "$table" | cut -d'.' -f2)

    # Get metrics
    file_count=$(spark-sql -e "SELECT COUNT(*) FROM $table.files" 2>/dev/null | tail -n 1 || echo "0")
    snapshot_count=$(spark-sql -e "SELECT COUNT(*) FROM $table.snapshots" 2>/dev/null | tail -n 1 || echo "0")
    avg_size=$(spark-sql -e "SELECT ROUND(AVG(file_size_in_bytes)/(1024*1024), 2) FROM $table.files" 2>/dev/null | tail -n 1 || echo "0")

    needs_compaction=false
    reasons=()

    if [ "$snapshot_count" -gt 15 ]; then
        needs_compaction=true
        reasons+=("$snapshot_count snapshots (>15)")
    fi

    if [ $(echo "$avg_size < 80" | bc -l 2>/dev/null || echo "0") -eq 1 ] && [ "$file_count" -gt 100 ]; then
        needs_compaction=true
        reasons+=("small files (avg ${avg_size}MB)")
    fi

    if [ "$needs_compaction" = true ]; then
        echo "  ⚠️  $table_name: NEEDS COMPACTION"
        for reason in "${reasons[@]}"; do
            echo "      - $reason"
        done
        echo "      Run: ./32_run_compaction_local.sh --table $table_name"
    else
        echo "  ✓ $table_name: OK"
    fi
done

echo ""
echo "To compact tables, run:"
echo "  ./32_run_compaction_local.sh                    # All tables"
echo "  ./32_run_compaction_local.sh --table TABLE_NAME # Specific table"
echo "  ./32_run_compaction_local.sh --dry-run          # Test mode"
echo ""
echo "Log files:"
LOG_DIR="/var/log/iceberg-compaction"
if [ -d "$LOG_DIR" ]; then
    echo "  Location: $LOG_DIR"
    echo "  Recent logs:"
    ls -lht "$LOG_DIR" 2>/dev/null | head -n 6 | tail -n 5 || echo "    (none)"
else
    echo "  Not configured (run ./33_setup_compaction_cron.sh)"
fi

echo "=============================================================================="
