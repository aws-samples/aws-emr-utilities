#!/bin/bash

# HBase Cross-Region Copy All Tables Script
# 
# This script automates copying all HBase tables from one region to another by:
# 1. Listing all tables on the source cluster
# 2. Creating snapshots for each table
# 3. Exporting all snapshots to the destination bucket
#
# Requirements: HBase 2.x on Amazon EMR
#
# Usage: ./hbase-cross-region-copy-all-tables.sh <source_cluster_master> <source_bucket_path> <dest_bucket_path> [ssh_key]

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_section() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
}

# Arguments
SOURCE_CLUSTER_MASTER="$1"
SOURCE_BUCKET_PATH="$2"
DEST_BUCKET_PATH="$3"
SSH_KEY="${4:-~/.ssh/id_rsa}"
AUTO_CONFIRM=false

# Check for --yes flag
for arg in "$@"; do
    if [ "$arg" = "--yes" ] || [ "$arg" = "-y" ]; then
        AUTO_CONFIRM=true
    fi
done

# Validate arguments
if [ -z "$SOURCE_CLUSTER_MASTER" ] || [ -z "$SOURCE_BUCKET_PATH" ] || [ -z "$DEST_BUCKET_PATH" ]; then
    echo "Usage: $0 <source_cluster_master> <source_bucket_path> <dest_bucket_path> [ssh_key] [--yes]"
    echo ""
    echo "Arguments:"
    echo "  source_cluster_master  - EMR master node hostname or IP"
    echo "  source_bucket_path     - S3 path where HBase data currently resides"
    echo "  dest_bucket_path       - S3 path where data should be copied"
    echo "  ssh_key                - SSH key to access EMR cluster (default: ~/.ssh/id_rsa)"
    echo "  --yes, -y              - Skip confirmation prompt"
    echo ""
    echo "Example:"
    echo "  $0 ec2-3-85-123-155.compute-1.amazonaws.com \\"
    echo "     s3://source-bucket/hbase/ \\"
    echo "     s3://dest-bucket/hbase/ \\"
    echo "     ~/.ssh/my-key.pem --yes"
    exit 1
fi

print_section "HBase Cross-Region Copy All Tables"
echo "Source Cluster: $SOURCE_CLUSTER_MASTER"
echo "Source Path:    $SOURCE_BUCKET_PATH"
echo "Dest Path:      $DEST_BUCKET_PATH"
echo "SSH Key:        $SSH_KEY"
echo ""

# Verify SSH key exists
if [ ! -f "$SSH_KEY" ]; then
    print_error "SSH key not found: $SSH_KEY"
    exit 1
fi

# Test SSH connection
print_info "Testing SSH connection..."
if ! ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 hadoop@"$SOURCE_CLUSTER_MASTER" "echo 'Connection successful'" > /dev/null 2>&1; then
    print_error "Cannot connect to source cluster master node"
    exit 1
fi
print_info "SSH connection successful"
echo ""

# Step 1: List all tables
print_section "Step 1: Listing all tables"
TABLES=$(ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
    "echo 'list' | hbase shell 2>&1" | grep -E '^[a-zA-Z_][a-zA-Z0-9_]*$' | grep -v 'TABLE')

if [ -z "$TABLES" ]; then
    print_error "No tables found on source cluster"
    exit 1
fi

TABLE_ARRAY=($TABLES)
TABLE_COUNT=${#TABLE_ARRAY[@]}

print_info "Found $TABLE_COUNT table(s):"
for table in "${TABLE_ARRAY[@]}"; do
    echo "  - $table"
done
echo ""

# Confirm with user
if [ "$AUTO_CONFIRM" = false ]; then
    read -p "Do you want to proceed with copying all $TABLE_COUNT table(s)? (yes/no): " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        print_info "Operation cancelled by user"
        exit 0
    fi
else
    print_info "Auto-confirm enabled, proceeding with copy..."
fi
echo ""

# Generate timestamp for snapshots
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Step 2: Create snapshots for all tables
print_section "Step 2: Creating snapshots"
SNAPSHOT_LIST=()

for table in "${TABLE_ARRAY[@]}"; do
    SNAPSHOT_NAME="snap_${table}_${TIMESTAMP}"
    print_info "Creating snapshot for table '$table': $SNAPSHOT_NAME"
    
    ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
        "echo \"snapshot '$table', '$SNAPSHOT_NAME'\" | hbase shell" > /dev/null 2>&1
    
    # Verify snapshot was created
    SNAPSHOT_CHECK=$(ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
        "echo \"list_snapshots '$SNAPSHOT_NAME'\" | hbase shell 2>&1 | grep -c '$SNAPSHOT_NAME' || echo 0")
    
    if [ "$SNAPSHOT_CHECK" -eq 0 ]; then
        print_error "Failed to create snapshot for table '$table'"
        continue
    fi
    
    SNAPSHOT_LIST+=("$SNAPSHOT_NAME")
    print_info "✓ Snapshot created: $SNAPSHOT_NAME"
done

echo ""
print_info "Created ${#SNAPSHOT_LIST[@]} snapshot(s)"
echo ""

# Step 3: Export all snapshots
print_section "Step 3: Exporting snapshots to destination"

SUCCESSFUL_EXPORTS=0
FAILED_EXPORTS=0

for SNAPSHOT_NAME in "${SNAPSHOT_LIST[@]}"; do
    print_info "Exporting snapshot: $SNAPSHOT_NAME"
    
    # Run export in background
    ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
        "sudo -u hbase nohup hbase snapshot export \
        -Dfs.s3a.etag.checksum.enabled=true \
        -snapshot $SNAPSHOT_NAME \
        -copy-to $DEST_BUCKET_PATH \
        -mappers 200 > /tmp/export-$SNAPSHOT_NAME.log 2>&1 &"
    
    # Wait for job to start
    sleep 5
    
    # Get the YARN application ID
    APP_ID=""
    for i in {1..30}; do
        APP_ID=$(ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
            "yarn application -list 2>&1 | grep ExportSnapshot | tail -1 | awk '{print \$1}'" || echo "")
        
        if [ -n "$APP_ID" ] && [ "$APP_ID" != "Application-Id" ]; then
            print_info "Export job started: $APP_ID"
            break
        fi
        
        if [ $i -eq 30 ]; then
            print_error "Export job did not start for $SNAPSHOT_NAME"
            print_error "Check log: ssh to cluster and run: cat /tmp/export-$SNAPSHOT_NAME.log"
            FAILED_EXPORTS=$((FAILED_EXPORTS + 1))
            continue 2
        fi
        
        sleep 2
    done
    
    # Monitor export progress
    MONITOR_ATTEMPTS=0
    MAX_MONITOR_ATTEMPTS=360  # 1 hour max (360 * 10 seconds)
    
    while true; do
        JOB_STATUS=$(ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
            "yarn application -status $APP_ID 2>&1" || echo "SSH_ERROR")
        
        if echo "$JOB_STATUS" | grep -q "SSH_ERROR\|Connection refused\|Connection timed out"; then
            print_error "SSH connection error while monitoring $SNAPSHOT_NAME"
            FAILED_EXPORTS=$((FAILED_EXPORTS + 1))
            break
        fi
        
        STATE=$(echo "$JOB_STATUS" | grep -E "^\s*State :" | awk '{print $3}')
        PROGRESS=$(echo "$JOB_STATUS" | grep -E "^\s*Progress :" | awk '{print $3}')
        
        if [ -z "$STATE" ]; then
            print_error "Unable to get job status for $SNAPSHOT_NAME (APP_ID: $APP_ID)"
            FAILED_EXPORTS=$((FAILED_EXPORTS + 1))
            break
        fi
        
        if [ "$STATE" = "FINISHED" ] || [ "$STATE" = "FAILED" ] || [ "$STATE" = "KILLED" ]; then
            FINAL_STATE=$(echo "$JOB_STATUS" | grep -E "^\s*Final-State :" | awk '{print $3}')
            
            if [ "$FINAL_STATE" = "SUCCEEDED" ]; then
                print_info "✓ Export completed: $SNAPSHOT_NAME"
                SUCCESSFUL_EXPORTS=$((SUCCESSFUL_EXPORTS + 1))
            else
                print_error "Export failed: $SNAPSHOT_NAME (State: $FINAL_STATE)"
                FAILED_EXPORTS=$((FAILED_EXPORTS + 1))
            fi
            break
        fi
        
        echo "  Progress: $PROGRESS% (State: $STATE)"
        
        MONITOR_ATTEMPTS=$((MONITOR_ATTEMPTS + 1))
        if [ $MONITOR_ATTEMPTS -ge $MAX_MONITOR_ATTEMPTS ]; then
            print_error "Monitoring timeout for $SNAPSHOT_NAME after 1 hour"
            FAILED_EXPORTS=$((FAILED_EXPORTS + 1))
            break
        fi
        
        sleep 10
    done
    
    echo ""
done

# Summary
print_section "Copy Summary"
echo "Total tables:       $TABLE_COUNT"
echo "Snapshots created:  ${#SNAPSHOT_LIST[@]}"
echo "Successful exports: $SUCCESSFUL_EXPORTS"
echo "Failed exports:     $FAILED_EXPORTS"
echo ""
echo "Source:      $SOURCE_BUCKET_PATH"
echo "Destination: $DEST_BUCKET_PATH"
echo ""

if [ $FAILED_EXPORTS -gt 0 ]; then
    print_warning "Some exports failed. Check logs on cluster: /tmp/export-*.log"
fi

print_info "Next steps:"
echo "1. Launch EMR cluster in destination region pointing to: $DEST_BUCKET_PATH"
echo "2. Import snapshots on the new cluster using hbase-import-snapshots.sh"
echo ""
print_section "Operation Complete"
