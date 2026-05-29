#!/usr/bin/env python3
"""
Event Log Discovery Job (Step 1 of Pipeline)
============================================
Scans S3 for RECENT event logs and adds them to backlog_events_log_v5 table.

Execution Order: 1 (FIRST JOB - must run before orchestrator)
Schedule: Every 30 minutes to 1 hour
Runtime: ~30 seconds to 2 minutes (fast due to time-based filtering)

Purpose:
- Discover RECENT event logs in S3 (modified within lookback window)
- Add new logs to backlog_events_log_v5 table with status='N'
- Skip old files (outside DISCOVERY_LOOKBACK_HOURS window)
- Skip .inprogress files (incomplete uploads)
- Generate unique ID for each log to prevent duplicates

Environment Variables:
- DISCOVERY_LOOKBACK_HOURS: Only discover files modified in last N hours (default: 2)
- S3_BUCKET: S3 bucket name
- S3_PREFIX: S3 prefix to scan (default: "logs/")
- BACKLOG_TABLE: Iceberg table name

Output:
- Inserts new records into backlog_events_log_v5 table
- Prints statistics about discovered logs (including skipped old files)
"""

import boto3
import hashlib
import os
import sys
from datetime import datetime, timezone, timedelta
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, lit, current_timestamp
from pyspark.sql.types import StructType, StructField, StringType, DateType, IntegerType, TimestampType, LongType

# ============================================================================
# Configuration
# ============================================================================

# S3 Configuration
S3_BUCKET = os.getenv("S3_BUCKET", "${S3_BUCKET}")
S3_PREFIX = os.getenv("S3_PREFIX", "logs/")

# Table Configuration
BACKLOG_TABLE = os.getenv("BACKLOG_TABLE", "${CATALOG_NAMESPACE}.backlog_events_log_v5")
ICEBERG_WAREHOUSE = os.getenv("ICEBERG_WAREHOUSE", f"s3://{S3_BUCKET}/iceberg/")

# Discovery Time Window (in hours)
# Only discover logs modified in the last N hours
DISCOVERY_LOOKBACK_HOURS = int(os.getenv("DISCOVERY_LOOKBACK_HOURS", "2"))  # Default: 2 hours

# AWS Configuration
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_PROFILE = os.getenv("AWS_PROFILE", "data-test")

# ============================================================================
# Spark Session Initialization
# ============================================================================

def get_spark_session():
    """Initialize Spark session with Iceberg Hive catalog support."""
    print("Initializing Spark session with Iceberg Hive catalog support...")

    spark = SparkSession.builder \
        .appName("EventLogDiscovery") \
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions") \
        .config("spark.sql.catalog.spark_catalog", "org.apache.iceberg.spark.SparkCatalog") \
        .config("spark.sql.catalog.spark_catalog.type", "hive") \
        .config("spark.sql.catalog.spark_catalog.warehouse", ICEBERG_WAREHOUSE) \
        .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
        .enableHiveSupport() \
        .getOrCreate()

    # Set log level
    spark.sparkContext.setLogLevel("WARN")

    print(f"✓ Spark session initialized: {spark.version}")
    print(f"   Catalog: Hive Iceberg")
    print(f"   Warehouse: {ICEBERG_WAREHOUSE}")
    return spark


# ============================================================================
# Event Log Discovery
# ============================================================================

def generate_uuid(s3_full_path):
    """
    Generate unique UUID from S3 path using SHA256 hash.

    Args:
        s3_full_path: Full S3 URI (e.g., s3://bucket/logs/app_id/)

    Returns:
        str: 32-character hash (first 32 chars of SHA256)
    """
    return hashlib.sha256(s3_full_path.encode()).hexdigest()[:32]


def generate_app_id_hash(application_id):
    """
    Generate hash from application_id using SHA256.
    This hash is used to join backlog and advisor tables.

    Args:
        application_id: Spark application ID (e.g., application_1234567890123_0001)

    Returns:
        str: 32-character hash (first 32 chars of SHA256)
    """
    if not application_id:
        return ""
    return hashlib.sha256(application_id.encode()).hexdigest()[:32]


def discover_event_logs_from_s3(s3_bucket, s3_prefix):
    """
    Scan S3 and discover recent event log files/folders.
    Only discovers logs modified within DISCOVERY_LOOKBACK_HOURS.

    Args:
        s3_bucket: S3 bucket name
        s3_prefix: S3 prefix to scan (e.g., 'logs/')

    Returns:
        list: List of dicts with event log metadata
    """
    print("=" * 80)
    print("DISCOVERING EVENT LOGS FROM S3")
    print("=" * 80)
    print(f"S3 Bucket: {s3_bucket}")
    print(f"S3 Prefix: {s3_prefix}")
    print(f"Scanning: s3://{s3_bucket}/{s3_prefix}")
    print(f"Lookback window: {DISCOVERY_LOOKBACK_HOURS} hours")
    print("-" * 80)

    s3 = boto3.client("s3", region_name=AWS_REGION)
    paginator = s3.get_paginator("list_objects_v2")

    discovered_apps = {}  # app_id -> metadata
    total_files = 0
    skipped_inprogress = 0
    skipped_old_files = 0

    now = datetime.now(timezone.utc)
    discovery_date = now.date()
    discovery_hour = now.hour  # Extract hour (0-23)

    # Calculate cutoff time for discovery
    cutoff_time = now - timedelta(hours=DISCOVERY_LOOKBACK_HOURS)

    print(f"Discovery timestamp: {now.isoformat()}")
    print(f"Only discovering files modified after: {cutoff_time.isoformat()}")
    print(f"Listing objects from S3 (this may take a few minutes)...")

    for page in paginator.paginate(Bucket=s3_bucket, Prefix=s3_prefix):
        if "Contents" not in page:
            continue

        for obj in page["Contents"]:
            key = obj["Key"]
            total_files += 1

            # Skip .inprogress files
            if key.endswith(".inprogress"):
                skipped_inprogress += 1
                continue

            # Skip non-event-log files
            if not ("application_" in key or "eventlog_v2_" in key):
                continue

            # Skip old files (outside lookback window)
            last_modified = obj["LastModified"]
            if last_modified < cutoff_time:
                skipped_old_files += 1
                continue

            # Extract application folder from key
            # Pattern 1: logs/application_1234567890123_0001/events_1_*.gz
            # Pattern 2: logs/eventlog_v2_application_1234567890123_0001/events_*.gz
            # Pattern 3: logs/application_1234567890123_0001.gz (single file)

            parts = key.split("/")
            if len(parts) < 2:
                continue

            app_folder = parts[1]  # e.g., "application_1234567890123_0001"
            is_rolling = app_folder.startswith("eventlog_v2_")

            # Extract clean application_id
            if is_rolling:
                application_id = app_folder.replace("eventlog_v2_", "")
            else:
                # Remove compression extensions
                application_id = app_folder.replace(".gz", "").replace(".lz4", "").replace(".zstd", "")

            # Skip if already discovered (multiple files in same app folder)
            if application_id in discovered_apps:
                continue

            # Construct full S3 path
            if is_rolling:
                # For rolling logs, use directory path
                s3_full_path = f"s3://{s3_bucket}/{s3_prefix}{app_folder}/"
                event_log_filename = ""  # Rolling logs have multiple files
            else:
                # For single file, use file path
                s3_full_path = f"s3://{s3_bucket}/{key}"
                event_log_filename = parts[-1]

            uuid = generate_uuid(s3_full_path)
            app_id_hash = generate_app_id_hash(application_id)

            discovered_apps[application_id] = {
                "uuid": uuid,
                "application_id": application_id,
                "app_id_hash": app_id_hash,
                "s3path": s3_full_path,
                "file_size": obj["Size"],
                "discovery_date": discovery_date,
                "discovery_hour": discovery_hour,
                "created_at": now,
                "is_processed": "N"
            }

    discovered_logs = list(discovered_apps.values())

    print("-" * 80)
    print(f"✓ Scanned {total_files:,} total files in S3")
    print(f"✓ Skipped {skipped_inprogress:,} .inprogress files")
    print(f"✓ Skipped {skipped_old_files:,} old files (outside {DISCOVERY_LOOKBACK_HOURS}h window)")
    print(f"✓ Discovered {len(discovered_logs):,} unique event logs (applications)")
    print("=" * 80)

    # Show sample
    if discovered_logs:
        print("\nSample discovered logs (first 5):")
        for i, log in enumerate(discovered_logs[:5], 1):
            print(f"  {i}. {log['application_id']}")
            print(f"     S3: {log['s3path']}")
        print()

    return discovered_logs


# ============================================================================
# Insert into Backlog Table (Append-Only)
# ============================================================================

def insert_into_backlog_table(spark, discovered_logs, backlog_table):
    """
    Insert discovered logs into backlog table (append-only mode).
    No deduplication - anti-join with advisor table handles duplicate processing.

    Args:
        spark: SparkSession
        discovered_logs: List of dicts with event log metadata
        backlog_table: Full table name (e.g., ${CATALOG_NAMESPACE}.backlog_events_log_v5)

    Returns:
        int: Number of records inserted
    """
    if not discovered_logs:
        print("No logs discovered to insert")
        return 0

    print("=" * 80)
    print("INSERTING INTO BACKLOG TABLE (APPEND-ONLY)")
    print("=" * 80)
    print(f"Table: {backlog_table}")
    print(f"Discovered logs: {len(discovered_logs):,}")
    print("-" * 80)

    # Check if table exists
    try:
        existing_count = spark.sql(f"SELECT COUNT(*) as cnt FROM {backlog_table}").collect()[0].cnt
        print(f"Existing records in table: {existing_count:,}")
    except Exception as e:
        print(f"⚠ Warning: Could not count existing records: {e}")
        print("Table may not exist yet. It will be created on first insert.")
        existing_count = 0

    # Create DataFrame from discovered logs with explicit schema
    print(f"Creating DataFrame from {len(discovered_logs):,} discovered logs...")

    # Define explicit schema to match table DDL exactly
    schema = StructType([
        StructField("uuid", StringType(), True),
        StructField("application_id", StringType(), True),
        StructField("app_id_hash", StringType(), True),
        StructField("s3path", StringType(), True),
        StructField("file_size", LongType(), True),
        StructField("discovery_date", DateType(), True),
        StructField("discovery_hour", IntegerType(), True),
        StructField("created_at", TimestampType(), True),
        StructField("is_processed", StringType(), True)
    ])

    discovered_df = spark.createDataFrame(discovered_logs, schema=schema)

    print(f"✓ DataFrame created with {discovered_df.count():,} records")

    # INSERT: Append-only mode (no deduplication)
    # Anti-join with advisor table handles duplicate processing
    print("Inserting new logs (append-only mode)...")

    try:
        discovered_df.writeTo(backlog_table).append()
        print("✓ INSERT completed successfully")
    except Exception as e:
        print(f"✗ INSERT failed: {e}")
        raise

    # Get new count
    new_count = spark.sql(f"SELECT COUNT(*) as cnt FROM {backlog_table}").collect()[0].cnt
    inserted_count = len(discovered_logs)

    print("-" * 80)
    print(f"✓ Inserted {inserted_count:,} new records")
    print(f"✓ Total records in {backlog_table}: {new_count:,}")
    print("=" * 80)

    return inserted_count


# ============================================================================
# Statistics and Reporting
# ============================================================================

def show_backlog_stats(spark, backlog_table):
    """Show current backlog statistics."""
    print("\n" + "=" * 80)
    print("BACKLOG TABLE STATISTICS")
    print("=" * 80)

    try:
        # Count by status
        print("\nStatus Distribution:")
        print("-" * 80)
        status_df = spark.sql(f"""
            SELECT
                is_processed,
                COUNT(*) as count,
                ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 2) as percentage
            FROM {backlog_table}
            GROUP BY is_processed
            ORDER BY
                CASE is_processed
                    WHEN 'N' THEN 1
                    WHEN 'Y' THEN 2
                    ELSE 3
                END
        """)
        status_df.show(truncate=False)

        # Count pending logs
        pending_count = spark.sql(f"""
            SELECT COUNT(*) as cnt
            FROM {backlog_table}
            WHERE is_processed = 'N'
        """).collect()[0].cnt

        print(f"\n📋 PENDING LOGS (ready for processing): {pending_count:,}")

        # Show oldest pending log
        oldest_pending = spark.sql(f"""
            SELECT
                discovery_date,
                discovery_hour,
                created_at,
                application_id,
                s3path
            FROM {backlog_table}
            WHERE is_processed = 'N'
            ORDER BY discovery_date ASC, discovery_hour ASC, created_at ASC
            LIMIT 1
        """)

        if oldest_pending.count() > 0:
            print("\n⏰ OLDEST PENDING LOG:")
            oldest_pending.show(truncate=False)

        # Show processing stats
        processing_stats = spark.sql(f"""
            SELECT
                COUNT(CASE WHEN is_processed = 'N' THEN 1 END) as pending,
                COUNT(CASE WHEN is_processed = 'Y' THEN 1 END) as processed,
                COUNT(*) as total
            FROM {backlog_table}
        """)

        print("\n📊 PROCESSING STATS:")
        processing_stats.show(truncate=False)

        # Show discovery by hour stats
        hourly_stats = spark.sql(f"""
            SELECT
                discovery_date,
                discovery_hour,
                COUNT(*) as log_count,
                SUM(CASE WHEN is_processed = 'N' THEN 1 ELSE 0 END) as pending_count
            FROM {backlog_table}
            WHERE discovery_date >= CURRENT_DATE - INTERVAL 1 DAY
            GROUP BY discovery_date, discovery_hour
            ORDER BY discovery_date DESC, discovery_hour DESC
            LIMIT 24
        """)

        print("\n📊 HOURLY DISCOVERY STATS (Last 24 Hours):")
        hourly_stats.show(truncate=False)

    except Exception as e:
        print(f"⚠ Warning: Could not retrieve statistics: {e}")

    print("=" * 80)


# ============================================================================
# Main Execution
# ============================================================================

def main():
    """Main discovery job execution."""
    start_time = datetime.now(timezone.utc)

    print("\n" + "=" * 80)
    print("EVENT LOG DISCOVERY JOB")
    print("=" * 80)
    print(f"Start Time: {start_time.isoformat()}")
    print(f"S3 Bucket: {S3_BUCKET}")
    print(f"S3 Prefix: {S3_PREFIX}")
    print(f"Backlog Table: {BACKLOG_TABLE}")
    print(f"Iceberg Warehouse: {ICEBERG_WAREHOUSE}")
    print("=" * 80)

    spark = None

    try:
        # Step 1: Initialize Spark
        spark = get_spark_session()

        # Step 2: Discover event logs from S3
        discovered_logs = discover_event_logs_from_s3(S3_BUCKET, S3_PREFIX)

        # Step 3: Insert into backlog table (append-only)
        inserted_count = insert_into_backlog_table(spark, discovered_logs, BACKLOG_TABLE)

        # Step 4: Show statistics
        show_backlog_stats(spark, BACKLOG_TABLE)

        # Calculate duration
        end_time = datetime.now(timezone.utc)
        duration = (end_time - start_time).total_seconds()

        print("\n" + "=" * 80)
        print("✅ DISCOVERY JOB COMPLETED SUCCESSFULLY")
        print("=" * 80)
        print(f"Start Time:     {start_time.isoformat()}")
        print(f"End Time:       {end_time.isoformat()}")
        print(f"Duration:       {duration:.1f} seconds ({duration/60:.1f} minutes)")
        print(f"Discovered:     {len(discovered_logs):,} event logs")
        print(f"New Logs Added: {inserted_count:,}")
        print("=" * 80)

        return 0

    except Exception as e:
        print("\n" + "=" * 80)
        print("❌ DISCOVERY JOB FAILED")
        print("=" * 80)
        print(f"Error: {e}")
        print("=" * 80)
        import traceback
        traceback.print_exc()
        return 1

    finally:
        if spark:
            print("\nStopping Spark session...")
            spark.stop()
            print("✓ Spark session stopped")


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
