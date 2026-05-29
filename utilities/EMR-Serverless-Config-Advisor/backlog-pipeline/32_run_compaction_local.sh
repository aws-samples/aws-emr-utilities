#!/bin/bash
##############################################################################
# Local Compaction Script for EMR EC2
##############################################################################
# Runs Iceberg table compaction directly on EMR EC2 master node using
# spark-submit. No EMR Serverless submission needed.
#
# Prerequisites:
#   - Run on EMR master node
#   - Spark installed (EMR 6.x or 7.x)
#   - IAM role with S3 and Glue access
#
# Usage:
#   # Compact all tables
#   ./32_run_compaction_local.sh
#
#   # Compact specific table
#   ./32_run_compaction_local.sh --table backlog_events_log_v5
#
#   # Dry run (no changes)
#   ./32_run_compaction_local.sh --dry-run
#
#   # With custom resources
#   ./32_run_compaction_local.sh --executor-memory 16g --num-executors 4
##############################################################################

set -e

# Configuration
AWS_REGION="${AWS_REGION:-us-east-1}"
S3_BUCKET="${S3_BUCKET:-${S3_BUCKET}}"
ICEBERG_WAREHOUSE="${ICEBERG_WAREHOUSE:-s3://${S3_BUCKET}/iceberg/}"

# Spark Configuration (adjust based on cluster size)
DRIVER_MEMORY="${DRIVER_MEMORY:-8g}"
EXECUTOR_MEMORY="${EXECUTOR_MEMORY:-16g}"
EXECUTOR_CORES="${EXECUTOR_CORES:-4}"
NUM_EXECUTORS="${NUM_EXECUTORS:-4}"

# Script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPACTION_SCRIPT="${SCRIPT_DIR}/30_compact_all_iceberg_tables.py"

# Parse arguments
TABLE_ARG=""
DRY_RUN_ARG=""
EXTRA_ARGS=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --table)
            TABLE_ARG="--table $2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN_ARG="--dry-run"
            shift
            ;;
        --driver-memory)
            DRIVER_MEMORY="$2"
            shift 2
            ;;
        --executor-memory)
            EXECUTOR_MEMORY="$2"
            shift 2
            ;;
        --executor-cores)
            EXECUTOR_CORES="$2"
            shift 2
            ;;
        --num-executors)
            NUM_EXECUTORS="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --table TABLE_NAME         Compact specific table only"
            echo "  --dry-run                  Show what would be done without changes"
            echo "  --driver-memory SIZE       Driver memory (default: 8g)"
            echo "  --executor-memory SIZE     Executor memory (default: 16g)"
            echo "  --executor-cores NUM       Cores per executor (default: 4)"
            echo "  --num-executors NUM        Number of executors (default: 4)"
            echo "  --help                     Show this help"
            echo ""
            echo "Valid table names:"
            echo "  - backlog_events_log_v5"
            echo "  - spark_metrics_task_stage_v5"
            echo "  - spark_metrics_config_v5"
            echo "  - serverless_config_advisor_v5"
            echo ""
            echo "Examples:"
            echo "  $0"
            echo "  $0 --table backlog_events_log_v5"
            echo "  $0 --dry-run"
            echo "  $0 --executor-memory 32g --num-executors 8"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Validate script exists
if [ ! -f "$COMPACTION_SCRIPT" ]; then
    echo "ERROR: Compaction script not found: $COMPACTION_SCRIPT"
    exit 1
fi

echo "=============================================================================="
echo "ICEBERG TABLE COMPACTION - LOCAL EXECUTION"
echo "=============================================================================="
echo "Execution Mode:    EMR EC2 (spark-submit)"
echo "Script:            $COMPACTION_SCRIPT"
echo "Table Filter:      ${TABLE_ARG:-All tables}"
echo "Mode:              ${DRY_RUN_ARG:-Production (will make changes)}"
echo ""
echo "Spark Configuration:"
echo "  Driver Memory:   $DRIVER_MEMORY"
echo "  Executor Memory: $EXECUTOR_MEMORY"
echo "  Executor Cores:  $EXECUTOR_CORES"
echo "  Num Executors:   $NUM_EXECUTORS"
echo ""
echo "Iceberg Configuration:"
echo "  Warehouse:       $ICEBERG_WAREHOUSE"
echo "  Region:          $AWS_REGION"
echo "=============================================================================="

# Build spark-submit arguments
PYTHON_ARGS=""
if [ -n "$TABLE_ARG" ]; then
    PYTHON_ARGS="$TABLE_ARG"
fi
if [ -n "$DRY_RUN_ARG" ]; then
    PYTHON_ARGS="$PYTHON_ARGS $DRY_RUN_ARG"
fi

# Set environment variables for Python script
export S3_BUCKET
export AWS_REGION
export ICEBERG_WAREHOUSE

echo ""
echo "Starting compaction..."
echo ""

# Run spark-submit
spark-submit \
    --master yarn \
    --deploy-mode client \
    --driver-memory "$DRIVER_MEMORY" \
    --executor-memory "$EXECUTOR_MEMORY" \
    --executor-cores "$EXECUTOR_CORES" \
    --num-executors "$NUM_EXECUTORS" \
    --conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions \
    --conf spark.sql.catalog.spark_catalog=org.apache.iceberg.spark.SparkCatalog \
    --conf spark.sql.catalog.spark_catalog.type=hive \
    --conf spark.sql.catalog.spark_catalog.warehouse="$ICEBERG_WAREHOUSE" \
    --conf spark.hadoop.fs.s3a.impl=org.apache.hadoop.fs.s3a.S3AFileSystem \
    --conf spark.sql.catalogImplementation=hive \
    --conf spark.hadoop.fs.s3a.connection.ssl.enabled=true \
    --conf spark.hadoop.fs.s3a.retry.limit=10 \
    --conf spark.hadoop.fs.s3a.retry.interval=1000 \
    --conf spark.dynamicAllocation.enabled=false \
    --jars /usr/share/aws/iceberg/lib/iceberg-spark3-runtime.jar \
    "$COMPACTION_SCRIPT" $PYTHON_ARGS

EXIT_CODE=$?

echo ""
echo "=============================================================================="
if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ Compaction completed successfully!"
else
    echo "✗ Compaction failed with exit code: $EXIT_CODE"
fi
echo "=============================================================================="

exit $EXIT_CODE
