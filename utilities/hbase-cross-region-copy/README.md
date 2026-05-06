# HBase Cross-Region Snapshot Copy

A utility to copy HBase snapshots from one S3 bucket to another across AWS regions while preserving data integrity using ETag checksums.

## Overview

This script automates the process of copying HBase snapshots between AWS regions using the HBase snapshot export feature with ETag checksum validation. It ensures data integrity during cross-region transfers without requiring manual S3 sync operations or disabling checksum verification.

## Features

- Data Integrity: Uses ETag checksums to validate data during transfer
- Cross-Region Support: Copy snapshots between any AWS regions
- Progress Monitoring: Real-time progress tracking of the export job
- Automatic Validation: Verifies snapshot exists before starting
- Error Handling: Comprehensive error checking and reporting

## Prerequisites

- HBase 2.x running on Amazon EMR
- Source cluster with HBase snapshot already created
- SSH access to the source EMR cluster master node
- AWS CLI configured with appropriate permissions
- IAM permissions for S3 operations on both source and destination buckets

**IMPORTANT: EMR Release Version Compatibility**

When migrating HBase operations to a different region, we strongly recommend using the same EMR release version on both source and destination clusters. This ensures compatibility and prevents potential issues with HBase metadata and data formats.

**IMPORTANT: Write Downtime Requirement**

All HBase writes to the source cluster must be stopped before taking the snapshot. Any writes that occur after the snapshot is taken will NOT be included in the copy and will be lost in the destination cluster.

If your use case cannot tolerate write downtime during the snapshot and copy process, please contact AWS Support to discuss alternative replication solutions.

## Usage

### Option 1: Copy a Single Snapshot

```bash
./hbase-cross-region-copy.sh <source_cluster_master> <source_bucket_path> <dest_bucket_path> <snapshot_name> [ssh_key]
```

#### Arguments

| Argument | Description | Required |
|----------|-------------|----------|
| `source_cluster_master` | EMR master node hostname or IP address | Yes |
| `source_bucket_path` | S3 path where HBase data currently resides | Yes |
| `dest_bucket_path` | S3 path where data should be copied | Yes |
| `snapshot_name` | Name of the HBase snapshot to copy (will be created if it doesn't exist) | Yes |
| `ssh_key` | SSH key to access EMR cluster | No (default: ~/.ssh/id_rsa) |

**Note:** If the snapshot doesn't exist, the script will automatically create it from the table name. The snapshot name should follow the format: `snap_<tablename>_<timestamp>` (e.g., `snap_my_table_20260302`).

#### Example

```bash
./hbase-cross-region-copy.sh \
  ec2-3-85-123-155.me-central-1.compute.amazonaws.com \
  s3://source-bucket-me-central-1/hbase/ \
  s3://dest-bucket-us-east-1/hbase/ \
  snap_my_table_20260302 \
  ~/.ssh/my-emr-key.pem
```

If the snapshot `snap_my_table_20260302` doesn't exist, the script will:
1. Extract the table name (`my_table`)
2. Create the snapshot from that table
3. Export it to the destination

### Option 2: Copy All Tables Automatically

For copying all tables in a cluster at once:

```bash
./hbase-cross-region-copy-all-tables.sh <source_cluster_master> <source_bucket_path> <dest_bucket_path> [ssh_key] [--yes]
```

This script will:
1. List all tables on the source cluster
2. Create snapshots for each table with timestamp
3. Export all snapshots to the destination bucket sequentially
4. Provide a summary of successful and failed exports

#### Arguments

| Argument | Description | Required |
|----------|-------------|----------|
| `source_cluster_master` | EMR master node hostname or IP address | Yes |
| `source_bucket_path` | S3 path where HBase data currently resides | Yes |
| `dest_bucket_path` | S3 path where data should be copied | Yes |
| `ssh_key` | SSH key to access EMR cluster | No (default: ~/.ssh/id_rsa) |
| `--yes` or `-y` | Skip confirmation prompt for non-interactive execution | No |

#### Example (Interactive)

```bash
./hbase-cross-region-copy-all-tables.sh \
  ec2-3-85-123-155.me-central-1.compute.amazonaws.com \
  s3://source-bucket-me-central-1/hbase/ \
  s3://dest-bucket-us-east-1/hbase/ \
  ~/.ssh/my-emr-key.pem
```

The script will prompt for confirmation before proceeding with the copy operation.

#### Example (Non-Interactive)

```bash
./hbase-cross-region-copy-all-tables.sh \
  ec2-3-85-123-155.me-central-1.compute.amazonaws.com \
  s3://source-bucket-me-central-1/hbase/ \
  s3://dest-bucket-us-east-1/hbase/ \
  ~/.ssh/my-emr-key.pem \
  --yes
```

Use the `--yes` flag to skip the confirmation prompt, useful for automation and scripting.

## How It Works

1. **Validation**: Checks if the specified snapshot exists on the source cluster
   - If snapshot doesn't exist, attempts to create it from the table name
   - Table name is extracted from snapshot name (format: `snap_<tablename>_<timestamp>`)
2. **Export**: Initiates HBase snapshot export with ETag checksum enabled
3. **Monitoring**: Tracks the MapReduce job progress in real-time
4. **Verification**: Confirms data was successfully copied to the destination

The script uses the `-Dfs.s3a.etag.checksum.enabled=true` flag to ensure that S3 ETags are used for checksum validation, preventing the "checksum mismatch" errors that can occur with cross-region S3 operations.

## Performance

The script uses HBase's MapReduce-based snapshot export, which runs with parallel mappers for efficient data transfer.

**Typical performance:**
- Speed: 2-3 GB/s (varies based on data size, network conditions, and number of mappers)
- Duration: ~10 minutes per 1.5 TiB of data

**Tuning for better performance:**

You can control the number of mappers to improve transfer speed by adding the `-mappers` flag:

```bash
# On the source cluster, modify the export command to use more mappers
sudo -u hbase hbase snapshot export \
  -Dfs.s3a.etag.checksum.enabled=true \
  -snapshot snap_name \
  -copy-to s3://dest-bucket/hbase/ \
  -mappers 200
```

Increasing the number of mappers can significantly improve throughput for large datasets, depending on your cluster size and available resources. Start with 100-200 mappers and adjust based on your cluster capacity.

## Post-Copy Steps: Importing Snapshots on Destination Cluster

After successfully copying snapshots to the destination region, follow these steps to restore your HBase tables on the destination cluster.

### Prerequisites for Import

1. **Launch destination EMR cluster** with HBase configured to use the destination S3 bucket:
   ```bash
   aws emr create-cluster \
     --name "HBase-Destination-Cluster" \
     --release-label emr-7.x.x \
     --applications Name=HBase Name=Hadoop \
     --instance-type m5.4xlarge \
     --instance-count 3 \
     --configurations '[{"Classification":"hbase","Properties":{"hbase.emr.storageMode":"s3"}},{"Classification":"hbase-site","Properties":{"hbase.rootdir":"s3://dest-bucket/hbase/"}}]'
   ```

2. **SSH to the destination cluster master node**

### Option 1: Automated Import (Recommended)

Use the companion import script to automatically restore all snapshots:

```bash
./hbase-import-snapshots.sh <dest_cluster_master> <snapshot_name> [ssh_key]
```

**Example:**
```bash
./hbase-import-snapshots.sh \
  ec2-13-127-73-255.ap-south-1.compute.amazonaws.com \
  snap_20260302_151152 \
  ~/.ssh/my-emr-key.pem
```

The script will:
1. List available snapshots on the destination cluster
2. Clone the snapshot to restore the table
3. Verify the table is accessible
4. Display table row count

### Option 2: Manual Import Steps

If you prefer manual control, follow these steps on the destination cluster:

**1. List available snapshots:**
```bash
echo "list_snapshots" | hbase shell
```

**2. Clone the snapshot to restore the table:**
```bash
echo "clone_snapshot 'snap_20260302_151152', 'restored_table_name'" | hbase shell
```

Or restore to the original table name:
```bash
echo "restore_snapshot 'snap_20260302_151152'" | hbase shell
```

**3. Verify the table:**
```bash
echo "list" | hbase shell
echo "count 'restored_table_name', INTERVAL => 10000" | hbase shell
```

**4. Enable the table (if needed):**
```bash
echo "enable 'restored_table_name'" | hbase shell
```

### Importing Multiple Tables

If you have multiple snapshots to import:

**1. List all snapshots:**
```bash
echo "list_snapshots" | hbase shell
```

**2. Create a script to import all:**
```bash
#!/bin/bash
SNAPSHOTS=("snap_table1_20260302" "snap_table2_20260302" "snap_table3_20260302")

for snap in "${SNAPSHOTS[@]}"; do
  echo "Restoring $snap..."
  echo "restore_snapshot '$snap'" | hbase shell
done
```

### Verification Steps

After importing, verify your data:

**1. Check table exists:**
```bash
echo "exists 'table_name'" | hbase shell
```

**2. Verify row count:**
```bash
echo "count 'table_name', INTERVAL => 10000" | hbase shell
```

**3. Sample data:**
```bash
echo "scan 'table_name', {LIMIT => 10}" | hbase shell
```

**4. Compare with source cluster:**
```bash
# On source cluster
echo "count 'table_name'" | hbase shell

# On destination cluster  
echo "count 'table_name'" | hbase shell
```

### Complete Workflow Example

Here's a complete end-to-end workflow:

**On Source Cluster:**
```bash
# 1. List tables
echo "list" | hbase shell

# 2. Create snapshots for all tables
echo "snapshot 'my_table', 'snap_my_table_20260302'" | hbase shell

# 3. Copy to destination region
./hbase-cross-region-copy.sh \
  ec2-source.me-central-1.compute.amazonaws.com \
  s3://source-bucket-me-central-1/hbase/ \
  s3://dest-bucket-us-east-1/hbase/ \
  snap_my_table_20260302 \
  ~/.ssh/source-key.pem
```

**On Destination Cluster:**
```bash
# 4. Import snapshot
./hbase-import-snapshots.sh \
  ec2-dest.us-east-1.compute.amazonaws.com \
  snap_my_table_20260302 \
  ~/.ssh/dest-key.pem

# 5. Verify data
echo "count 'my_table'" | hbase shell
```

## Troubleshooting

### Snapshot Not Found
If the script reports that the snapshot doesn't exist:
- Verify the snapshot name is correct
- Check that you're connected to the correct cluster
- List available snapshots: `echo "list_snapshots" | hbase shell`

### Export Job Fails
If the export job fails:
- Check `/tmp/export-output.log` for detailed error messages
- Verify IAM permissions for both source and destination buckets
- Ensure the destination bucket exists and is accessible

### SSH Connection Issues
If you can't connect to the cluster:
- Verify the SSH key path is correct
- Check security group rules allow SSH access
- Ensure the master node hostname/IP is correct

## IAM Permissions Required

The EMR cluster's IAM role needs the following S3 permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::source-bucket/*",
        "arn:aws:s3:::source-bucket",
        "arn:aws:s3:::dest-bucket/*",
        "arn:aws:s3:::dest-bucket"
      ]
    }
  ]
}
```

## References

- [AWS EMR HBase Best Practices - Data Integrity](https://aws.github.io/aws-emr-best-practices/docs/bestpractices/Applications/HBase/data_integrity)
- [HBase Snapshot Documentation](https://hbase.apache.org/book.html#ops.snapshots)
- [Amazon EMR HBase on S3](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-hbase-s3.html)

## License

This utility is licensed under the MIT-0 License. See the [LICENSE](../../LICENSE) file in the repository root.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.
