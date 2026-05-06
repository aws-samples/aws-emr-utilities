#!/bin/bash
#
# HBase Cross-Region Snapshot Copy Script
# 
# This script copies an HBase snapshot from one S3 bucket to another across AWS regions
# while preserving data integrity using ETag checksums.
#
# Requirements: HBase 2.x on Amazon EMR
#
# Usage: ./hbase-cross-region-copy.sh <source_cluster_master> <source_bucket_path> <dest_bucket_path> <snapshot_name> [ssh_key]
#
# Example:
#   ./hbase-cross-region-copy.sh \
#     ec2-3-85-123-155.compute-1.amazonaws.com \
#     s3://source-bucket/hbase/ \
#     s3://dest-bucket/hbase/ \
#     snap_20260302_151152 \
#     ~/.ssh/my-key.pem

set -e

# Arguments
SOURCE_CLUSTER_MASTER="$1"
SOURCE_BUCKET_PATH="$2"
DEST_BUCKET_PATH="$3"
SNAPSHOT_NAME="$4"
SSH_KEY="${5:-~/.ssh/id_rsa}"

# Validate arguments
if [ -z "$SOURCE_CLUSTER_MASTER" ] || [ -z "$SOURCE_BUCKET_PATH" ] || [ -z "$DEST_BUCKET_PATH" ] || [ -z "$SNAPSHOT_NAME" ]; then
    echo "Usage: $0 <source_cluster_master> <source_bucket_path> <dest_bucket_path> <snapshot_name> [ssh_key]"
    echo ""
    echo "Arguments:"
    echo "  source_cluster_master  - EMR master node hostname or IP"
    echo "  source_bucket_path     - S3 path where HBase data currently resides"
    echo "  dest_bucket_path       - S3 path where data should be copied"
    echo "  snapshot_name          - Name of the HBase snapshot to copy"
    echo "  ssh_key                - SSH key to access EMR cluster (default: ~/.ssh/id_rsa)"
    echo ""
    echo "Example:"
    echo "  $0 ec2-3-85-123-155.compute-1.amazonaws.com \\"
    echo "     s3://source-bucket/hbase/ \\"
    echo "     s3://dest-bucket/hbase/ \\"
    echo "     snap_20260302_151152 \\"
    echo "     ~/.ssh/my-key.pem"
    exit 1
fi

echo "=========================================="
echo "HBase Cross-Region Snapshot Copy"
echo "=========================================="
echo "Source Cluster: $SOURCE_CLUSTER_MASTER"
echo "Source Path:    $SOURCE_BUCKET_PATH"
echo "Dest Path:      $DEST_BUCKET_PATH"
echo "Snapshot:       $SNAPSHOT_NAME"
echo "SSH Key:        $SSH_KEY"
echo "=========================================="
echo ""

# Step 1: Check if snapshot exists, create if needed
echo "[1/4] Checking if snapshot exists..."
SNAPSHOT_EXISTS=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no hadoop@"$SOURCE_CLUSTER_MASTER" \
    "echo \"list_snapshots '$SNAPSHOT_NAME'\" | hbase shell 2>&1 | grep -c '$SNAPSHOT_NAME' || echo 0")

if [ "$SNAPSHOT_EXISTS" -eq 0 ]; then
    echo "Snapshot '$SNAPSHOT_NAME' not found"
    
    # Try to extract table name from snapshot name (format: snap_<tablename>_<timestamp>)
    TABLE_NAME=$(echo "$SNAPSHOT_NAME" | sed 's/^snap_//' | sed 's/_[0-9]*$//')
    
    echo "Attempting to create snapshot from table '$TABLE_NAME'..."
    
    # Check if table exists
    TABLE_EXISTS=$(ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
        "echo \"exists '$TABLE_NAME'\" | hbase shell 2>&1 | grep -c 'does exist' || echo 0")
    
    if [ "$TABLE_EXISTS" -eq 0 ]; then
        echo "ERROR: Table '$TABLE_NAME' not found on source cluster"
        echo "Available tables:"
        ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" "echo 'list' | hbase shell 2>&1 | grep -A 100 TABLE"
        echo ""
        echo "Please either:"
        echo "  1. Provide an existing snapshot name, or"
        echo "  2. Ensure the snapshot name follows format: snap_<tablename>_<timestamp>"
        exit 1
    fi
    
    # Create snapshot
    echo "Creating snapshot '$SNAPSHOT_NAME' from table '$TABLE_NAME'..."
    ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
        "echo \"snapshot '$TABLE_NAME', '$SNAPSHOT_NAME'\" | hbase shell" > /dev/null 2>&1
    
    # Verify snapshot was created
    SNAPSHOT_CHECK=$(ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
        "echo \"list_snapshots '$SNAPSHOT_NAME'\" | hbase shell 2>&1 | grep -c '$SNAPSHOT_NAME' || echo 0")
    
    if [ "$SNAPSHOT_CHECK" -eq 0 ]; then
        echo "ERROR: Failed to create snapshot"
        exit 1
    fi
    
    echo "✓ Snapshot created successfully"
else
    echo "✓ Snapshot exists"
fi
echo ""

# Step 2: Export snapshot to destination bucket
echo "[2/4] Exporting snapshot to destination bucket..."
echo "This may take several minutes depending on data size..."

# Run export in background and capture output
ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
    "sudo -u hbase nohup hbase snapshot export \
    -Dfs.s3a.etag.checksum.enabled=true \
    -snapshot $SNAPSHOT_NAME \
    -copy-to $DEST_BUCKET_PATH \
    -mappers 200 > /tmp/export-$SNAPSHOT_NAME.log 2>&1 &"

# Wait a moment for job to start
sleep 5

# Get the YARN application ID
echo "Waiting for export job to start..."
for i in {1..30}; do
    APP_ID=$(ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
        "yarn application -list 2>&1 | grep ExportSnapshot | tail -1 | awk '{print \$1}'" || echo "")
    
    if [ -n "$APP_ID" ]; then
        echo "✓ Export job started: $APP_ID"
        break
    fi
    
    if [ $i -eq 30 ]; then
        echo "ERROR: Export job did not start within expected time"
        echo "Check logs: ssh to cluster and run: cat /tmp/export-$SNAPSHOT_NAME.log"
        exit 1
    fi
    
    sleep 2
done
echo ""

# Step 3: Monitor export progress
echo "[3/4] Monitoring export progress..."
while true; do
    JOB_STATUS=$(ssh -i "$SSH_KEY" hadoop@"$SOURCE_CLUSTER_MASTER" \
        "yarn application -status $APP_ID 2>&1")
    
    STATE=$(echo "$JOB_STATUS" | grep "State :" | awk '{print $3}')
    PROGRESS=$(echo "$JOB_STATUS" | grep "Progress :" | awk '{print $3}')
    
    if [ "$STATE" = "FINISHED" ] || [ "$STATE" = "FAILED" ] || [ "$STATE" = "KILLED" ]; then
        FINAL_STATE=$(echo "$JOB_STATUS" | grep "Final-State :" | awk '{print $3}')
        
        if [ "$FINAL_STATE" = "SUCCEEDED" ]; then
            echo "✓ Export job completed successfully (100%)"
            break
        else
            echo "ERROR: Export job failed with state: $FINAL_STATE"
            echo "Check logs on cluster: cat /tmp/export-$SNAPSHOT_NAME.log"
            exit 1
        fi
    fi
    
    echo "Progress: $PROGRESS% (State: $STATE)"
    sleep 10
done
echo ""

# Step 4: Verify data copied
echo "[4/4] Verifying data in destination bucket..."
DEST_BUCKET=$(echo "$DEST_BUCKET_PATH" | sed 's|s3://||' | cut -d'/' -f1)
DEST_PREFIX=$(echo "$DEST_BUCKET_PATH" | sed 's|s3://||' | cut -d'/' -f2-)
DEST_REGION=$(aws s3api get-bucket-location --bucket "$DEST_BUCKET" --query 'LocationConstraint' --output text 2>/dev/null || echo "us-east-1")

if [ "$DEST_REGION" = "None" ] || [ -z "$DEST_REGION" ]; then
    DEST_REGION="us-east-1"
fi

DEST_SIZE=$(aws s3 ls "$DEST_BUCKET_PATH" --region "$DEST_REGION" --recursive --summarize 2>&1 | \
    grep "Total Size" | awk '{print $3, $4}')

if [ -z "$DEST_SIZE" ]; then
    echo "WARNING: Could not verify destination size. Please check manually."
else
    echo "✓ Data copied to destination: $DEST_SIZE"
fi
echo ""

echo "=========================================="
echo "COPY COMPLETED SUCCESSFULLY"
echo "=========================================="
echo "Source:      $SOURCE_BUCKET_PATH"
echo "Destination: $DEST_BUCKET_PATH"
echo "Snapshot:    $SNAPSHOT_NAME"
echo "Data Size:   $DEST_SIZE"
echo ""
echo "Next steps:"
echo "1. Launch EMR cluster in destination region pointing to: $DEST_BUCKET_PATH"
echo "2. Import snapshot on the new cluster using:"
echo ""
echo "   hbase snapshot import \\"
echo "     -Dfs.s3a.etag.checksum.enabled=true \\"
echo "     -snapshot $SNAPSHOT_NAME \\"
echo "     -copy-from $DEST_BUCKET_PATH \\"
echo "     -copy-to $DEST_BUCKET_PATH"
echo ""
echo "=========================================="
