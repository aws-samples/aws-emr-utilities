#!/usr/bin/env python3
"""
Iceberg Table Compaction Script
================================
Compacts all Iceberg tables in the EMR Serverless Config Advisor pipeline.

Purpose:
- Compacts small files into larger files (target: 130 MB)
- Removes old snapshots (keeps only last 10 snapshots)
- Improves query performance and reduces metadata overhead

Tables:
- backlog_events_log_v5: Backlog events table
- spark_metrics_task_stage_v5: Task and stage metrics
- spark_metrics_config_v5: Spark configuration details
- serverless_config_advisor_v5: Advisor recommendations

Configuration:
- Target file size: 130 MB (134217728 bytes)
- Max snapshots to keep: 10
- Removes all snapshots older than the most recent 10

Usage:
  # Compact all tables
  python3 30_compact_all_iceberg_tables.py

  # Compact specific table
  python3 30_compact_all_iceberg_tables.py --table backlog_events_log_v5

  # Dry run (no changes)
  python3 30_compact_all_iceberg_tables.py --dry-run

Environment Variables:
- S3_BUCKET: S3 bucket for Iceberg warehouse
- AWS_REGION: AWS region (default: us-east-1)
"""

import os
import sys
import argparse
from datetime import datetime, timezone
from pyspark.sql import SparkSession

# ============================================================================
# Configuration
# ============================================================================

# AWS Configuration
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "${S3_BUCKET}")
ICEBERG_WAREHOUSE = os.getenv("ICEBERG_WAREHOUSE", f"s3://{S3_BUCKET}/iceberg/")

# Compaction Configuration
TARGET_FILE_SIZE_BYTES = 134217728  # 130 MB
MAX_SNAPSHOTS_TO_KEEP = 3  # Keep only 3 most recent snapshots

# Table Configuration
# Note: All tables except backlog_events_log_v5 are CLUSTERED BY (job_id) INTO 100 BUCKETS
TABLES = {
    "backlog_events_log_v5": {
        "full_name": "${CATALOG_NAMESPACE}.backlog_events_log_v5",
        "description": "Backlog events table (partitioned)",
        "partition_by": ["discovery_date", "discovery_hour"],
        "clustered": False
    },
    "spark_metrics_task_stage_v5": {
        "full_name": "${CATALOG_NAMESPACE}.spark_metrics_task_stage_v5",
        "description": "Task and stage level metrics (clustered by job_id)",
        "partition_by": [],  # Not partitioned - uses clustering
        "clustered": True,
        "cluster_by": "job_id",
        "num_buckets": 100
    },
    "spark_metrics_config_v5": {
        "full_name": "${CATALOG_NAMESPACE}.spark_metrics_config_v5",
        "description": "Spark configuration details (clustered by job_id)",
        "partition_by": [],  # Not partitioned - uses clustering
        "clustered": True,
        "cluster_by": "job_id",
        "num_buckets": 100
    },
    "serverless_config_advisor_v5": {
        "full_name": "${CATALOG_NAMESPACE}.serverless_config_advisor_v5",
        "description": "Advisor recommendations (clustered by job_id)",
        "partition_by": [],  # Not partitioned - uses clustering
        "clustered": True,
        "cluster_by": "job_id",
        "num_buckets": 100
    }
}

# ============================================================================
# Spark Session Initialization
# ============================================================================

def get_spark_session():
    """Initialize Spark session with Iceberg support."""
    print("Initializing Spark session with Iceberg support...")

    spark = SparkSession.builder \
        .appName("Iceberg_Table_Compaction") \
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions") \
        .config("spark.sql.catalog.spark_catalog", "org.apache.iceberg.spark.SparkCatalog") \
        .config("spark.sql.catalog.spark_catalog.type", "hive") \
        .config("spark.sql.catalog.spark_catalog.warehouse", ICEBERG_WAREHOUSE) \
        .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
        .enableHiveSupport() \
        .getOrCreate()

    spark.sparkContext.setLogLevel("WARN")

    print(f"✓ Spark session initialized: {spark.version}")
    print(f"   Catalog: Hive Iceberg")
    print(f"   Warehouse: {ICEBERG_WAREHOUSE}")
    return spark


# ============================================================================
# Table Information Functions
# ============================================================================

def get_table_info(spark, table_name):
    """Get current table statistics."""
    try:
        # Get table metadata
        df = spark.sql(f"SELECT COUNT(*) as row_count FROM {table_name}")
        row_count = df.collect()[0]['row_count']

        # Get snapshot information
        snapshots_df = spark.sql(f"SELECT * FROM {table_name}.snapshots ORDER BY committed_at DESC")
        snapshot_count = snapshots_df.count()

        # Get file information
        files_df = spark.sql(f"SELECT * FROM {table_name}.files")
        file_count = files_df.count()

        if file_count > 0:
            total_size = files_df.selectExpr("SUM(file_size_in_bytes) as total_size").collect()[0]['total_size']
            avg_file_size = total_size / file_count if file_count > 0 else 0
        else:
            total_size = 0
            avg_file_size = 0

        return {
            'row_count': row_count,
            'snapshot_count': snapshot_count,
            'file_count': file_count,
            'total_size_bytes': total_size,
            'avg_file_size_mb': avg_file_size / (1024 ** 2) if avg_file_size > 0 else 0
        }
    except Exception as e:
        print(f"  ⚠ Could not get table info: {e}")
        return None


# ============================================================================
# Compaction Functions
# ============================================================================

def compact_table(spark, table_name, target_file_size, dry_run=False):
    """
    Compact table data files.

    Args:
        spark: SparkSession
        table_name: Full table name (e.g., ${CATALOG_NAMESPACE}.backlog_events_log_v5)
        target_file_size: Target file size in bytes
        dry_run: If True, only show what would be done

    Returns:
        bool: True if successful, False otherwise
    """
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Compacting table: {table_name}")
    print(f"  Target file size: {target_file_size / (1024 ** 2):.2f} MB")

    try:
        if not dry_run:
            # Set table property for target file size
            spark.sql(f"""
                ALTER TABLE {table_name}
                SET TBLPROPERTIES (
                    'write.target-file-size-bytes' = '{target_file_size}'
                )
            """)
            print(f"  ✓ Set target file size property")

            # Run compaction using CALL procedure
            # This rewrites data files to match the target size
            spark.sql(f"CALL spark_catalog.system.rewrite_data_files('{table_name}')")
            print(f"  ✓ Compaction completed")
        else:
            print(f"  → Would set target file size to {target_file_size / (1024 ** 2):.2f} MB")
            print(f"  → Would run: CALL spark_catalog.system.rewrite_data_files('{table_name}')")

        return True

    except Exception as e:
        print(f"  ✗ Compaction failed: {e}")
        return False


def expire_snapshots(spark, table_name, max_snapshots_to_keep, dry_run=False):
    """
    Remove old snapshots, keeping only the most recent N snapshots.

    Args:
        spark: SparkSession
        table_name: Full table name
        max_snapshots_to_keep: Number of recent snapshots to keep
        dry_run: If True, only show what would be done

    Returns:
        int: Number of snapshots expired
    """
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Expiring old snapshots: {table_name}")
    print(f"  Keeping most recent {max_snapshots_to_keep} snapshots")

    try:
        # Get all snapshots ordered by timestamp
        snapshots_df = spark.sql(f"""
            SELECT snapshot_id, committed_at, operation
            FROM {table_name}.snapshots
            ORDER BY committed_at DESC
        """)

        snapshots = snapshots_df.collect()
        total_snapshots = len(snapshots)

        print(f"  Current snapshot count: {total_snapshots}")

        if total_snapshots <= max_snapshots_to_keep:
            print(f"  ✓ No snapshots to expire (already <= {max_snapshots_to_keep})")
            return 0

        # Calculate which snapshots to expire
        snapshots_to_expire = total_snapshots - max_snapshots_to_keep

        # Get the timestamp of the Nth newest snapshot (to keep N newest)
        cutoff_snapshot = snapshots[max_snapshots_to_keep - 1]
        cutoff_timestamp = cutoff_snapshot['committed_at']

        print(f"  Snapshots to expire: {snapshots_to_expire}")
        print(f"  Cutoff timestamp: {cutoff_timestamp}")

        if not dry_run:
            # Set table property for metadata retention
            spark.sql(f"""
                ALTER TABLE {table_name}
                SET TBLPROPERTIES (
                    'write.metadata.previous-versions-max' = '{max_snapshots_to_keep}'
                )
            """)
            print(f"  ✓ Set metadata retention property")

            # Expire snapshots older than cutoff
            spark.sql(f"""
                CALL spark_catalog.system.expire_snapshots(
                    table => '{table_name}',
                    older_than => TIMESTAMP '{cutoff_timestamp}',
                    retain_last => {max_snapshots_to_keep}
                )
            """)
            print(f"  ✓ Expired {snapshots_to_expire} old snapshots")
        else:
            print(f"  → Would set 'write.metadata.previous-versions-max' = '{max_snapshots_to_keep}'")
            print(f"  → Would expire {snapshots_to_expire} snapshots older than {cutoff_timestamp}")

        return snapshots_to_expire

    except Exception as e:
        print(f"  ✗ Snapshot expiration failed: {e}")
        return 0


def remove_orphan_files(spark, table_name, dry_run=False):
    """
    Remove orphan files (data files no longer referenced by any snapshot).

    Args:
        spark: SparkSession
        table_name: Full table name
        dry_run: If True, only show what would be done

    Returns:
        bool: True if successful
    """
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Removing orphan files: {table_name}")

    try:
        if not dry_run:
            # Remove orphan files older than 3 days (safe default)
            result = spark.sql(f"""
                CALL spark_catalog.system.remove_orphan_files(
                    table => '{table_name}',
                    older_than => TIMESTAMP '{datetime.now(timezone.utc).isoformat()}'
                )
            """)

            orphan_count = result.collect()[0][0] if result.count() > 0 else 0
            print(f"  ✓ Removed {orphan_count} orphan files")
        else:
            print(f"  → Would remove orphan files older than now")

        return True

    except Exception as e:
        print(f"  ⚠ Orphan file removal warning: {e}")
        return False


# ============================================================================
# Main Compaction Orchestrator
# ============================================================================

def compact_single_table(spark, table_key, table_config, dry_run=False):
    """Compact a single table with all maintenance operations."""
    print("\n" + "=" * 80)
    print(f"TABLE: {table_config['full_name']}")
    print(f"Description: {table_config['description']}")

    # Display table structure
    if table_config.get('clustered', False):
        print(f"Structure: CLUSTERED BY ({table_config['cluster_by']}) INTO {table_config['num_buckets']} BUCKETS")
    elif table_config.get('partition_by'):
        print(f"Structure: PARTITIONED BY ({', '.join(table_config['partition_by'])})")
    else:
        print(f"Structure: Not partitioned or clustered")

    print("=" * 80)

    table_name = table_config['full_name']

    # Get table info before compaction
    print("\n📊 Current Table Statistics:")
    info_before = get_table_info(spark, table_name)
    if info_before:
        print(f"  Rows:             {info_before['row_count']:,}")
        print(f"  Data files:       {info_before['file_count']:,}")
        print(f"  Avg file size:    {info_before['avg_file_size_mb']:.2f} MB")
        print(f"  Total size:       {info_before['total_size_bytes'] / (1024 ** 3):.2f} GB")
        print(f"  Snapshots:        {info_before['snapshot_count']}")

    # Step 1: Compact data files
    compact_success = compact_table(spark, table_name, TARGET_FILE_SIZE_BYTES, dry_run)

    # Step 2: Expire old snapshots
    expired_count = expire_snapshots(spark, table_name, MAX_SNAPSHOTS_TO_KEEP, dry_run)

    # Step 3: Remove orphan files
    orphan_success = remove_orphan_files(spark, table_name, dry_run)

    # Get table info after compaction
    if not dry_run and compact_success:
        print("\n📊 Updated Table Statistics:")
        info_after = get_table_info(spark, table_name)
        if info_after:
            print(f"  Rows:             {info_after['row_count']:,}")
            print(f"  Data files:       {info_after['file_count']:,}")
            print(f"  Avg file size:    {info_after['avg_file_size_mb']:.2f} MB")
            print(f"  Total size:       {info_after['total_size_bytes'] / (1024 ** 3):.2f} GB")
            print(f"  Snapshots:        {info_after['snapshot_count']}")

            # Calculate improvements
            if info_before:
                file_reduction = info_before['file_count'] - info_after['file_count']
                file_reduction_pct = (file_reduction / info_before['file_count'] * 100) if info_before['file_count'] > 0 else 0
                snapshot_reduction = info_before['snapshot_count'] - info_after['snapshot_count']

                print(f"\n✨ Improvements:")
                print(f"  Files reduced:    {file_reduction:,} ({file_reduction_pct:.1f}%)")
                print(f"  Avg file size:    {info_before['avg_file_size_mb']:.2f} MB → {info_after['avg_file_size_mb']:.2f} MB")
                print(f"  Snapshots removed: {snapshot_reduction}")

    print("=" * 80)

    return compact_success


def compact_all_tables(spark, specific_table=None, dry_run=False):
    """Compact all tables or a specific table."""
    start_time = datetime.now(timezone.utc)

    print("\n" + "=" * 80)
    print("ICEBERG TABLE COMPACTION")
    print("=" * 80)
    print(f"Start time:           {start_time.isoformat()}")
    print(f"Target file size:     {TARGET_FILE_SIZE_BYTES / (1024 ** 2):.0f} MB")
    print(f"Max snapshots kept:   {MAX_SNAPSHOTS_TO_KEEP}")
    print(f"Mode:                 {'DRY RUN (no changes)' if dry_run else 'PRODUCTION'}")
    if specific_table:
        print(f"Target:               {specific_table} only")
    else:
        print(f"Target:               All {len(TABLES)} tables")
    print("=" * 80)

    # Filter tables if specific table requested
    tables_to_compact = {}
    if specific_table:
        if specific_table in TABLES:
            tables_to_compact[specific_table] = TABLES[specific_table]
        else:
            print(f"\n❌ Error: Table '{specific_table}' not found")
            print(f"Available tables: {', '.join(TABLES.keys())}")
            return False
    else:
        tables_to_compact = TABLES

    # Compact each table
    results = {}
    for table_key, table_config in tables_to_compact.items():
        try:
            success = compact_single_table(spark, table_key, table_config, dry_run)
            results[table_key] = 'SUCCESS' if success else 'FAILED'
        except Exception as e:
            print(f"\n❌ Error compacting {table_key}: {e}")
            results[table_key] = 'ERROR'

    # Summary
    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()

    print("\n" + "=" * 80)
    print("COMPACTION SUMMARY")
    print("=" * 80)
    print(f"Start time:     {start_time.isoformat()}")
    print(f"End time:       {end_time.isoformat()}")
    print(f"Duration:       {duration:.1f} seconds")
    print(f"Tables processed: {len(results)}")
    print("\nResults:")
    for table_key, status in results.items():
        icon = "✓" if status == "SUCCESS" else "✗"
        print(f"  {icon} {table_key}: {status}")

    success_count = sum(1 for status in results.values() if status == 'SUCCESS')
    print(f"\n✓ Successful: {success_count}/{len(results)}")
    print("=" * 80)

    return success_count == len(results)


# ============================================================================
# Main Execution
# ============================================================================

def main():
    """Main execution."""
    parser = argparse.ArgumentParser(
        description='Compact Iceberg tables in EMR Serverless Config Advisor pipeline'
    )
    parser.add_argument(
        '--table',
        help='Specific table to compact (default: all tables)',
        choices=list(TABLES.keys())
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes'
    )

    args = parser.parse_args()

    spark = None
    try:
        # Initialize Spark
        spark = get_spark_session()

        # Run compaction
        success = compact_all_tables(spark, args.table, args.dry_run)

        return 0 if success else 1

    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        if spark:
            print("\nStopping Spark session...")
            spark.stop()
            print("✓ Spark session stopped")


if __name__ == "__main__":
    sys.exit(main())
