#!/bin/bash
##############################################################################
# Setup Cron Jobs for Automated Compaction on EMR EC2
##############################################################################
# Sets up cron jobs to run compaction automatically on EMR master node.
#
# Usage:
#   # Setup default schedule (daily backlog, weekly metrics)
#   ./33_setup_compaction_cron.sh
#
#   # Setup custom schedule
#   ./33_setup_compaction_cron.sh --schedule custom
#
#   # Remove cron jobs
#   ./33_setup_compaction_cron.sh --remove
#
#   # Show current schedule
#   ./33_setup_compaction_cron.sh --show
##############################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPACTION_SCRIPT="${SCRIPT_DIR}/32_run_compaction_local.sh"
CRON_COMMENT="# EMR Iceberg Compaction Jobs"
LOG_DIR="/var/log/iceberg-compaction"

# Validate script exists
if [ ! -f "$COMPACTION_SCRIPT" ]; then
    echo "ERROR: Compaction script not found: $COMPACTION_SCRIPT"
    exit 1
fi

# Create log directory
if [ ! -d "$LOG_DIR" ]; then
    echo "Creating log directory: $LOG_DIR"
    sudo mkdir -p "$LOG_DIR"
    sudo chown hadoop:hadoop "$LOG_DIR"
fi

# Function to show current cron jobs
show_cron() {
    echo "=============================================================================="
    echo "CURRENT COMPACTION CRON JOBS"
    echo "=============================================================================="
    if crontab -l 2>/dev/null | grep -A 10 "$CRON_COMMENT" >/dev/null 2>&1; then
        crontab -l | grep -A 10 "$CRON_COMMENT"
    else
        echo "No compaction cron jobs found"
    fi
    echo "=============================================================================="
}

# Function to remove cron jobs
remove_cron() {
    echo "Removing compaction cron jobs..."

    # Get current crontab without compaction jobs
    if crontab -l 2>/dev/null | grep -v "$CRON_COMMENT" | grep -v "run_compaction_local.sh" > /tmp/crontab.tmp; then
        crontab /tmp/crontab.tmp
        rm /tmp/crontab.tmp
        echo "✓ Compaction cron jobs removed"
    else
        echo "No compaction cron jobs to remove"
    fi
}

# Function to setup default cron schedule
setup_default_cron() {
    echo "=============================================================================="
    echo "SETTING UP DEFAULT COMPACTION SCHEDULE"
    echo "=============================================================================="
    echo ""
    echo "Schedule:"
    echo "  Daily (2 AM):   backlog_events_log_v5"
    echo "  Weekly (Sun 3 AM): All tables"
    echo ""
    echo "Logs will be written to: $LOG_DIR"
    echo "=============================================================================="

    # Get existing crontab
    crontab -l 2>/dev/null > /tmp/crontab.tmp || true

    # Remove old compaction jobs if they exist
    sed -i.bak "/$CRON_COMMENT/,+10d" /tmp/crontab.tmp 2>/dev/null || true

    # Add new cron jobs
    cat >> /tmp/crontab.tmp << EOF

$CRON_COMMENT
# Daily compaction for backlog table (2 AM)
0 2 * * * $COMPACTION_SCRIPT --table backlog_events_log_v5 >> $LOG_DIR/backlog_daily.log 2>&1

# Weekly compaction for all tables (Sunday 3 AM)
0 3 * * 0 $COMPACTION_SCRIPT >> $LOG_DIR/all_tables_weekly.log 2>&1

EOF

    # Install new crontab
    crontab /tmp/crontab.tmp
    rm /tmp/crontab.tmp

    echo ""
    echo "✓ Cron jobs installed successfully!"
    echo ""
    show_cron
}

# Function to setup aggressive cron schedule (for high-write workloads)
setup_aggressive_cron() {
    echo "=============================================================================="
    echo "SETTING UP AGGRESSIVE COMPACTION SCHEDULE"
    echo "=============================================================================="
    echo ""
    echo "Schedule:"
    echo "  Every 6 hours:  backlog_events_log_v5"
    echo "  Daily (3 AM):   All metrics tables"
    echo "  Weekly (Sun 4 AM): All tables"
    echo ""
    echo "Logs will be written to: $LOG_DIR"
    echo "=============================================================================="

    # Get existing crontab
    crontab -l 2>/dev/null > /tmp/crontab.tmp || true

    # Remove old compaction jobs if they exist
    sed -i.bak "/$CRON_COMMENT/,+20d" /tmp/crontab.tmp 2>/dev/null || true

    # Add new cron jobs
    cat >> /tmp/crontab.tmp << EOF

$CRON_COMMENT
# Aggressive: Compact backlog table every 6 hours
0 */6 * * * $COMPACTION_SCRIPT --table backlog_events_log_v5 >> $LOG_DIR/backlog_6hourly.log 2>&1

# Daily compaction for metrics tables (3 AM)
0 3 * * * $COMPACTION_SCRIPT --table spark_metrics_task_stage_v5 >> $LOG_DIR/metrics_task_stage_daily.log 2>&1
0 3 * * * $COMPACTION_SCRIPT --table spark_metrics_config_v5 >> $LOG_DIR/metrics_config_daily.log 2>&1
0 3 * * * $COMPACTION_SCRIPT --table serverless_config_advisor_v5 >> $LOG_DIR/advisor_daily.log 2>&1

# Weekly full compaction (Sunday 4 AM)
0 4 * * 0 $COMPACTION_SCRIPT >> $LOG_DIR/all_tables_weekly.log 2>&1

EOF

    # Install new crontab
    crontab /tmp/crontab.tmp
    rm /tmp/crontab.tmp

    echo ""
    echo "✓ Cron jobs installed successfully!"
    echo ""
    show_cron
}

# Function to setup minimal cron schedule (for low-write workloads)
setup_minimal_cron() {
    echo "=============================================================================="
    echo "SETTING UP MINIMAL COMPACTION SCHEDULE"
    echo "=============================================================================="
    echo ""
    echo "Schedule:"
    echo "  Weekly (Sun 2 AM): All tables"
    echo ""
    echo "Logs will be written to: $LOG_DIR"
    echo "=============================================================================="

    # Get existing crontab
    crontab -l 2>/dev/null > /tmp/crontab.tmp || true

    # Remove old compaction jobs if they exist
    sed -i.bak "/$CRON_COMMENT/,+10d" /tmp/crontab.tmp 2>/dev/null || true

    # Add new cron jobs
    cat >> /tmp/crontab.tmp << EOF

$CRON_COMMENT
# Weekly compaction for all tables (Sunday 2 AM)
0 2 * * 0 $COMPACTION_SCRIPT >> $LOG_DIR/all_tables_weekly.log 2>&1

EOF

    # Install new crontab
    crontab /tmp/crontab.tmp
    rm /tmp/crontab.tmp

    echo ""
    echo "✓ Cron jobs installed successfully!"
    echo ""
    show_cron
}

# Main execution
case "${1:-default}" in
    --show)
        show_cron
        ;;
    --remove)
        remove_cron
        ;;
    --schedule)
        case "${2:-default}" in
            default)
                setup_default_cron
                ;;
            aggressive)
                setup_aggressive_cron
                ;;
            minimal)
                setup_minimal_cron
                ;;
            *)
                echo "ERROR: Unknown schedule type: $2"
                echo "Valid options: default, aggressive, minimal"
                exit 1
                ;;
        esac
        ;;
    *)
        echo "Usage: $0 [OPTION]"
        echo ""
        echo "Options:"
        echo "  --show                    Show current cron schedule"
        echo "  --remove                  Remove all compaction cron jobs"
        echo "  --schedule default        Setup default schedule (recommended)"
        echo "  --schedule aggressive     Setup aggressive schedule (high-write workload)"
        echo "  --schedule minimal        Setup minimal schedule (low-write workload)"
        echo ""
        echo "Default behavior (no args): Setup default schedule"
        echo ""
        echo "Schedule Comparison:"
        echo "  minimal:    Weekly compaction only"
        echo "  default:    Daily backlog + Weekly full"
        echo "  aggressive: Every 6h backlog + Daily metrics + Weekly full"
        echo ""
        exit 0
        ;;
esac

exit 0
