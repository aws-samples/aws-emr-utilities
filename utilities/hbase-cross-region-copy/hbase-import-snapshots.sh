#!/bin/bash

# HBase Snapshot Import Script
# Imports HBase snapshots on the destination cluster after cross-region copy
# Requirements: HBase 2.x on Amazon EMR

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

# Check arguments
if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <dest_cluster_master> <snapshot_name> [ssh_key]"
    echo ""
    echo "Arguments:"
    echo "  dest_cluster_master  - Destination EMR master node hostname or IP"
    echo "  snapshot_name        - Name of the snapshot to import"
    echo "  ssh_key             - SSH key to access destination cluster (optional, default: ~/.ssh/id_rsa)"
    echo ""
    echo "Example:"
    echo "  $0 ec2-13-127-73-255.ap-south-1.compute.amazonaws.com snap_20260302_151152 ~/.ssh/my-key.pem"
    exit 1
fi

DEST_CLUSTER_MASTER=$1
SNAPSHOT_NAME=$2
SSH_KEY=${3:-~/.ssh/id_rsa}

print_info "Starting HBase snapshot import process..."
print_info "Destination Cluster: $DEST_CLUSTER_MASTER"
print_info "Snapshot Name: $SNAPSHOT_NAME"
print_info "SSH Key: $SSH_KEY"

# Verify SSH key exists
if [ ! -f "$SSH_KEY" ]; then
    print_error "SSH key not found: $SSH_KEY"
    exit 1
fi

# Test SSH connection
print_info "Testing SSH connection to destination cluster..."
if ! ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 hadoop@"$DEST_CLUSTER_MASTER" "echo 'Connection successful'" > /dev/null 2>&1; then
    print_error "Cannot connect to destination cluster master node"
    print_error "Please verify:"
    print_error "  1. Master node hostname/IP is correct"
    print_error "  2. SSH key has correct permissions (chmod 400)"
    print_error "  3. Security group allows SSH access"
    exit 1
fi
print_info "SSH connection successful"

# Check if snapshot exists
print_info "Checking if snapshot exists on destination cluster..."
SNAPSHOT_CHECK=$(ssh -i "$SSH_KEY" hadoop@"$DEST_CLUSTER_MASTER" "echo \"list_snapshots '$SNAPSHOT_NAME'\" | hbase shell 2>&1" || true)

if echo "$SNAPSHOT_CHECK" | grep -q "0 row(s)"; then
    print_error "Snapshot '$SNAPSHOT_NAME' not found on destination cluster"
    print_error "Please ensure the snapshot was successfully copied using hbase-cross-region-copy.sh"
    exit 1
fi
print_info "Snapshot '$SNAPSHOT_NAME' found"

# Extract table name from snapshot (assuming format: snap_<tablename>_<timestamp>)
TABLE_NAME=$(echo "$SNAPSHOT_NAME" | sed 's/^snap_//' | sed 's/_[0-9]*$//')
print_info "Detected table name: $TABLE_NAME"

# Check if table already exists
print_info "Checking if table '$TABLE_NAME' already exists..."
TABLE_EXISTS=$(ssh -i "$SSH_KEY" hadoop@"$DEST_CLUSTER_MASTER" "echo \"exists '$TABLE_NAME'\" | hbase shell 2>&1" || true)

if echo "$TABLE_EXISTS" | grep -q "does exist"; then
    print_warning "Table '$TABLE_NAME' already exists on destination cluster"
    read -p "Do you want to drop and recreate it? (yes/no): " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        print_info "Skipping table restoration"
        exit 0
    fi
    
    print_info "Disabling and dropping existing table..."
    ssh -i "$SSH_KEY" hadoop@"$DEST_CLUSTER_MASTER" "echo \"disable '$TABLE_NAME'\" | hbase shell" || true
    ssh -i "$SSH_KEY" hadoop@"$DEST_CLUSTER_MASTER" "echo \"drop '$TABLE_NAME'\" | hbase shell" || true
fi

# Clone snapshot to restore table
print_info "Restoring table from snapshot..."
RESTORE_OUTPUT=$(ssh -i "$SSH_KEY" hadoop@"$DEST_CLUSTER_MASTER" "echo \"clone_snapshot '$SNAPSHOT_NAME', '$TABLE_NAME'\" | hbase shell 2>&1")

if echo "$RESTORE_OUTPUT" | grep -q "ERROR"; then
    print_error "Failed to restore snapshot"
    echo "$RESTORE_OUTPUT"
    exit 1
fi
print_info "Table restored successfully"

# Verify table is accessible
print_info "Verifying table is accessible..."
TABLE_CHECK=$(ssh -i "$SSH_KEY" hadoop@"$DEST_CLUSTER_MASTER" "echo \"exists '$TABLE_NAME'\" | hbase shell 2>&1")

if ! echo "$TABLE_CHECK" | grep -q "does exist"; then
    print_error "Table verification failed"
    exit 1
fi
print_info "Table verification successful"

# Get row count (with timeout for large tables)
print_info "Getting table row count (this may take a while for large tables)..."
ROW_COUNT=$(ssh -i "$SSH_KEY" hadoop@"$DEST_CLUSTER_MASTER" "timeout 60 bash -c \"echo \\\"count '$TABLE_NAME', INTERVAL => 10000\\\" | hbase shell 2>&1\" || echo 'Count timed out (table is large)'" || echo "Count failed")

if echo "$ROW_COUNT" | grep -q "row(s)"; then
    ROWS=$(echo "$ROW_COUNT" | grep "row(s)" | tail -1)
    print_info "Table row count: $ROWS"
elif echo "$ROW_COUNT" | grep -q "timed out"; then
    print_warning "Row count timed out - table is likely very large"
else
    print_warning "Could not get row count"
fi

# Summary
echo ""
print_info "========================================="
print_info "Snapshot Import Complete!"
print_info "========================================="
print_info "Snapshot: $SNAPSHOT_NAME"
print_info "Table: $TABLE_NAME"
print_info "Cluster: $DEST_CLUSTER_MASTER"
echo ""
print_info "Next steps:"
print_info "1. Verify data: echo \"scan '$TABLE_NAME', {LIMIT => 10}\" | hbase shell"
print_info "2. Compare row counts with source cluster"
print_info "3. Run application tests against the new table"
echo ""
