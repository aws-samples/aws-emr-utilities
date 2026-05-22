#!/usr/bin/env python3
"""
Read advisor recommendations from S3 (last 5 partitions) and write to Iceberg + DynamoDB.

Usage:
  spark-submit 08_s3_to_iceberg_dynamodb.py \
    --s3-path s3://${S3_BUCKET}/emr-serverless-config-advisor/ \
    --iceberg-table ${CATALOG_NAMESPACE}.serverless_config_advisor_v5 \
    --dynamodb-table test-dynamodb-egdataplatform-dataproc-emr-serverless-config-recommander \
    --num-partitions 5

This script:
  1. Lists all datehour partitions in S3
  2. Sorts by datehour (yyyymmddHH) and takes the N most recent
  3. Reads JSON files from those partitions
  4. Writes to Iceberg table
  5. Writes to DynamoDB table
"""

import argparse
import os
import sys
from datetime import datetime
from decimal import Decimal
import boto3
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, lit

# AWS Region
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


def get_recent_partitions(s3_path, num_partitions=5):
    """
    List all datehour partitions in S3 and return the N most recent.

    Args:
        s3_path: S3 path to advisor data (e.g., s3://bucket/emr-serverless-config-advisor/)
        num_partitions: Number of recent partitions to return

    Returns:
        list: List of datehour values (integers) sorted by timestamp (most recent first)
    """
    print("=" * 80)
    print("DISCOVERING RECENT PARTITIONS IN S3")
    print("=" * 80)
    print(f"S3 Path: {s3_path}")
    print(f"Number of partitions to retrieve: {num_partitions}")
    print("-" * 80)

    # Parse S3 path
    s3_path_clean = s3_path.replace("s3://", "").rstrip("/")
    parts = s3_path_clean.split("/", 1)
    bucket = parts[0]
    prefix = parts[1] + "/" if len(parts) > 1 else ""

    print(f"Bucket: {bucket}")
    print(f"Prefix: {prefix}")
    print("-" * 80)

    # List all partitions
    s3_client = boto3.client('s3', region_name=AWS_REGION)
    paginator = s3_client.get_paginator('list_objects_v2')

    partitions = set()

    print("Scanning S3 for partitions...")
    page_iterator = paginator.paginate(
        Bucket=bucket,
        Prefix=prefix,
        Delimiter='/'
    )

    for page in page_iterator:
        # Get common prefixes (partitions)
        for common_prefix in page.get('CommonPrefixes', []):
            prefix_path = common_prefix['Prefix']
            # Extract datehour from path like "emr-serverless-config-advisor/datehour=2026051907/"
            if 'datehour=' in prefix_path:
                datehour_str = prefix_path.split('datehour=')[1].rstrip('/')
                try:
                    datehour = int(datehour_str)
                    partitions.add(datehour)
                except ValueError:
                    print(f"  ⚠ Skipping invalid partition: {prefix_path}")

    if not partitions:
        print("✗ No partitions found!")
        return []

    # Sort by datehour (most recent first)
    sorted_partitions = sorted(partitions, reverse=True)

    print(f"✓ Found {len(sorted_partitions)} partitions")
    print(f"\nAll partitions (sorted by timestamp, newest first):")
    for i, p in enumerate(sorted_partitions[:10], 1):  # Show first 10
        print(f"  {i}. datehour={p}")
    if len(sorted_partitions) > 10:
        print(f"  ... and {len(sorted_partitions) - 10} more")

    # Take the N most recent
    recent_partitions = sorted_partitions[:num_partitions]

    print(f"\n✓ Selected {len(recent_partitions)} most recent partitions:")
    for i, p in enumerate(recent_partitions, 1):
        print(f"  {i}. datehour={p}")

    print("=" * 80)
    return recent_partitions


def read_partitions_from_s3(spark, s3_path, partitions):
    """
    Read JSON files from specified partitions.

    Args:
        spark: SparkSession
        s3_path: S3 base path
        partitions: List of datehour values (integers)

    Returns:
        DataFrame: Combined data from all partitions
    """
    print("=" * 80)
    print("READING DATA FROM S3 PARTITIONS")
    print("=" * 80)

    if not partitions:
        print("✗ No partitions to read")
        return None

    # Build S3 paths for each partition
    s3_base = s3_path.rstrip('/')
    partition_paths = [f"{s3_base}/datehour={p}/" for p in partitions]

    print(f"Reading from {len(partition_paths)} partitions:")
    for path in partition_paths:
        print(f"  - {path}")
    print("-" * 80)

    # Read JSON files from all partitions
    # Spark writes JSON in JSON Lines format (one JSON object per line)
    # Let Spark infer schema automatically, then cast columns
    try:
        print("Reading JSON files...")
        print(f"  Format: JSON Lines (one object per line)")
        print(f"  Mode: PERMISSIVE (skip corrupt records)")

        # Read with inferred schema - use .format("json").load() to avoid path parsing issues
        df = spark.read \
            .format("json") \
            .option("mode", "PERMISSIVE") \
            .option("multiLine", "false") \
            .load(partition_paths)

        print("✓ JSON files loaded, inferring schema...")

        # Cast columns to correct types (only if they exist)
        numeric_columns = [
            "input_gb", "shuffle_read_gb", "shuffle_write_gb",
            "peak_shuffle_write_per_stage", "peak_disk_spill_per_stage",
            "duration_hours", "duration_minutes",
            "avg_memory_utilization_percent", "avg_cpu_utilization_percent",
            "max_memory_utilization_percent", "idle_core_percentage",
            "total_memory_spilled_gb", "cost_factor", "datehour"
        ]

        # Check available columns
        available_cols = df.columns
        print(f"Available columns: {available_cols}")

        # Cast only existing columns
        for col_name in numeric_columns:
            if col_name in available_cols:
                df = df.withColumn(col_name, col(col_name).cast("double"))

        print(f"✓ Cast {len([c for c in numeric_columns if c in available_cols])} numeric columns to double")

        # Rename columns for DynamoDB compatibility
        if "Job_id" in df.columns:
            df = df.withColumnRenamed("Job_id", "job_id")
            print("✓ Renamed 'Job_id' to 'job_id' for DynamoDB compatibility")

        if "created_at" in df.columns:
            df = df.withColumnRenamed("created_at", "created_date")
            print("✓ Renamed 'created_at' to 'created_date' for DynamoDB compatibility")

        # Cache to avoid re-reading
        df.cache()

        # Check for required columns (after renaming)
        required_cols = ["job_id", "app_id", "created_date"]
        missing_cols = [c for c in required_cols if c not in df.columns]
        if missing_cols:
            print(f"✗ Error: Missing required columns: {missing_cols}")
            print(f"  Available columns: {df.columns}")
            return None

        record_count = df.count()

        if record_count == 0:
            print("⚠ Warning: No records found in the specified partitions")
            print("  Partitions may be empty or files may not exist")
            print("=" * 80)
            return df  # Return empty DataFrame instead of None

        print(f"✓ Read {record_count:,} valid records from S3")

        # Show sample data
        print("\nSample data (first 3 rows):")
        sample_cols = ["job_id", "app_id", "created_date"]
        if "datehour" in df.columns:
            sample_cols.insert(2, "datehour")
        df.select(*sample_cols).show(3, truncate=80)

        # Show schema
        print("\nDataFrame Schema:")
        df.printSchema()

        print("=" * 80)
        return df

    except Exception as e:
        print(f"✗ Error reading from S3: {e}")
        import traceback
        traceback.print_exc()
        print("=" * 80)
        return None


def write_to_iceberg(df, iceberg_table, spark):
    """
    Write data to Iceberg table.

    Args:
        df: DataFrame to write
        iceberg_table: Iceberg table name (e.g., ${CATALOG_NAMESPACE}.serverless_config_advisor_v5)
        spark: SparkSession
    """
    print("=" * 80)
    print("WRITING TO ICEBERG TABLE")
    print("=" * 80)
    print(f"Table: {iceberg_table}")
    print(f"Records: {df.count():,}")
    print("-" * 80)

    # Select columns to match Iceberg schema
    # Rename created_date back to created_at for Iceberg (Iceberg expects created_at)
    iceberg_df = df.select(
        col("job_id"),
        col("application_name"),
        col("app_id"),
        col("app_id_hash"),
        col("optimization_mode"),
        col("input_gb"),
        col("shuffle_read_gb"),
        col("shuffle_write_gb"),
        col("peak_shuffle_write_per_stage"),
        col("peak_disk_spill_per_stage"),
        col("duration_hours"),
        col("duration_minutes"),
        col("avg_memory_utilization_percent"),
        col("avg_cpu_utilization_percent"),
        col("max_memory_utilization_percent"),
        col("idle_core_percentage"),
        col("total_memory_spilled_gb"),
        col("cost_factor"),
        col("src_event_log_location"),
        col("cost_config"),
        col("perf_config"),
        col("created_date").alias("created_at")  # Rename back for Iceberg
    )

    # Deduplicate by job_id and created_at (keep most recent based on sort)
    # Note: We renamed created_date back to created_at for Iceberg
    print("Deduplicating records by job_id + created_at...")
    dedup_df = iceberg_df.dropDuplicates(["job_id", "created_at"])
    original_count = df.count()
    dedup_count = dedup_df.count()
    duplicates_removed = original_count - dedup_count
    print(f"  Original records: {original_count:,}")
    print(f"  After dedup: {dedup_count:,}")
    print(f"  Duplicates removed: {duplicates_removed:,}")

    try:
        # Check if table exists
        try:
            spark.sql(f"DESCRIBE TABLE {iceberg_table}")
            print(f"✓ Table {iceberg_table} exists")
        except Exception:
            print(f"⚠ Table {iceberg_table} does not exist - it will be created")

        # Write to Iceberg table (append mode)
        print("Writing to Iceberg...")
        dedup_df.writeTo(iceberg_table).append()

        print(f"✓ Successfully wrote {dedup_count:,} records to Iceberg")
        print("=" * 80)
        return True

    except Exception as e:
        print(f"✗ Error writing to Iceberg: {e}")
        import traceback
        traceback.print_exc()
        print("=" * 80)
        return False


def write_to_dynamodb(df, dynamodb_table):
    """
    Write data to DynamoDB table.

    Args:
        df: DataFrame to write
        dynamodb_table: DynamoDB table name
    """
    print("=" * 80)
    print("WRITING TO DYNAMODB TABLE")
    print("=" * 80)
    print(f"Table: {dynamodb_table}")
    print(f"Records: {df.count():,}")
    print("-" * 80)

    # Deduplicate by job_id and created_date before collecting
    # Note: DataFrame has created_date (renamed from created_at for DynamoDB)
    print("Deduplicating records by job_id + created_date...")
    dedup_df = df.dropDuplicates(["job_id", "created_date"])
    original_count = df.count()
    dedup_count = dedup_df.count()
    print(f"  Original records: {original_count:,}")
    print(f"  After dedup: {dedup_count:,}")
    print(f"  Duplicates removed: {original_count - dedup_count:,}")

    # Convert DataFrame to list of records
    print("Collecting records from DataFrame...")
    records = dedup_df.collect()

    # Initialize DynamoDB client
    try:
        dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
        table = dynamodb.Table(dynamodb_table)

        # Verify table exists
        table.load()
        print(f"✓ Connected to DynamoDB table: {dynamodb_table}")
    except Exception as e:
        print(f"✗ Failed to connect to DynamoDB table: {e}")
        return 0

    CHUNK_SIZE = 500  # Process records in chunks of 500 (batch_writer handles 25-item API calls internally)

    print(f"Writing {len(records):,} items to DynamoDB in chunks of {CHUNK_SIZE}...")
    print(f"  API batching: up to 25 items per BatchWriteItem call (AWS limit)")
    print(f"  Total API calls estimate: ~{len(records) // 25 + 1:,}")

    write_count = 0
    failed_count = 0
    failed_items = []

    def prepare_item(record):
        """Convert Spark Row to DynamoDB-ready dict."""
        item = record.asDict()

        # Convert None to empty strings / zeros
        for key in item:
            if item[key] is None:
                if key in ['job_id', 'created_date', 'app_id_hash']:
                    item[key] = ""
                else:
                    item[key] = 0.0

        # Convert floats to Decimal for DynamoDB
        for key, value in item.items():
            if isinstance(value, float):
                item[key] = Decimal(str(value))

        # Remove datehour field (not in DynamoDB schema)
        item.pop('datehour', None)

        return item

    # Process in chunks of 500 using batch_writer (25 items per API call internally)
    for chunk_start in range(0, len(records), CHUNK_SIZE):
        chunk = records[chunk_start:chunk_start + CHUNK_SIZE]
        chunk_end = min(chunk_start + CHUNK_SIZE, len(records))

        try:
            with table.batch_writer() as batch:
                for record in chunk:
                    try:
                        item = prepare_item(record)

                        # Validate required fields
                        if not item.get('job_id') or not item.get('created_date'):
                            failed_count += 1
                            failed_items.append(f"job_id={item.get('job_id', 'missing')}, created_date={item.get('created_date', 'missing')}")
                            continue

                        batch.put_item(Item=item)
                        write_count += 1

                    except Exception as item_e:
                        failed_count += 1
                        if failed_count <= 10:
                            print(f"  ⚠ Failed to prepare item: {item_e}")
                        failed_items.append(f"job_id={record.asDict().get('job_id', 'unknown')}: {str(item_e)[:50]}")

            print(f"  Progress: {chunk_end:,}/{len(records):,} items written (chunk {chunk_start // CHUNK_SIZE + 1}/{(len(records) + CHUNK_SIZE - 1) // CHUNK_SIZE})...")

        except Exception as chunk_e:
            print(f"  ⚠ Chunk {chunk_start}-{chunk_end} failed: {chunk_e}")
            failed_count += len(chunk)
            failed_items.append(f"chunk {chunk_start}-{chunk_end}: {str(chunk_e)[:80]}")

    print(f"\n✓ Successfully wrote {write_count:,} items to DynamoDB")
    if failed_count > 0:
        print(f"⚠ Failed to write {failed_count:,} items")
        if failed_items and len(failed_items) <= 20:
            print(f"\nFirst {min(20, len(failed_items))} failures:")
            for item in failed_items[:20]:
                print(f"  - {item}")

    print("=" * 80)
    return write_count


def main():
    """Main execution."""
    parser = argparse.ArgumentParser(description='Read S3 partitions and write to Iceberg + DynamoDB')
    parser.add_argument('--s3-path', required=True, help='S3 path to advisor data')
    parser.add_argument('--iceberg-table', required=True, help='Iceberg table name')
    parser.add_argument('--dynamodb-table', required=True, help='DynamoDB table name')
    parser.add_argument('--num-partitions', type=int, default=1, help='Number of recent partitions to read (default: 1)')
    parser.add_argument('--s3-bucket', default='${S3_BUCKET}', help='S3 bucket for Iceberg warehouse')

    args = parser.parse_args()

    start_time = datetime.now()

    print("\n" + "=" * 80)
    print("S3 TO ICEBERG + DYNAMODB LOADER")
    print("=" * 80)
    print(f"Start Time: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"S3 Path: {args.s3_path}")
    print(f"Iceberg Table: {args.iceberg_table}")
    print(f"DynamoDB Table: {args.dynamodb_table}")
    print(f"Number of Partitions: {args.num_partitions}")
    print("=" * 80)

    # Initialize Spark session with Iceberg support
    print("\nInitializing Spark session...")
    iceberg_warehouse = f"s3://{args.s3_bucket}/iceberg/"

    spark = SparkSession.builder \
        .appName("S3_to_Iceberg_DynamoDB") \
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions") \
        .config("spark.sql.catalog.spark_catalog", "org.apache.iceberg.spark.SparkCatalog") \
        .config("spark.sql.catalog.spark_catalog.type", "hive") \
        .config("spark.sql.catalog.spark_catalog.warehouse", iceberg_warehouse) \
        .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
        .enableHiveSupport() \
        .getOrCreate()

    spark.sparkContext.setLogLevel("WARN")
    print(f"✓ Spark session initialized: {spark.version}\n")

    try:
        # Step 1: Discover recent partitions
        partitions = get_recent_partitions(args.s3_path, args.num_partitions)

        if not partitions:
            print("✗ No partitions found. Exiting.")
            return 1

        # Step 2: Read data from S3
        df = read_partitions_from_s3(spark, args.s3_path, partitions)

        if df is None or df.count() == 0:
            print("✗ No data read from S3. Exiting.")
            return 1

        # Step 3: Write to Iceberg
        iceberg_success = write_to_iceberg(df, args.iceberg_table, spark)

        # Step 4: Write to DynamoDB
        dynamodb_count = write_to_dynamodb(df, args.dynamodb_table)

        # Summary
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        print("\n" + "=" * 80)
        print("EXECUTION SUMMARY")
        print("=" * 80)
        print(f"Start Time:        {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"End Time:          {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Duration:          {duration:.1f} seconds")
        print(f"Partitions Read:   {len(partitions)}")
        print(f"Records Processed: {df.count():,}")
        print(f"Iceberg Write:     {'✓ Success' if iceberg_success else '✗ Failed'}")
        print(f"DynamoDB Write:    {dynamodb_count:,} records")
        print("=" * 80)

        return 0 if iceberg_success and dynamodb_count > 0 else 1

    except Exception as e:
        print(f"\n✗ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        spark.stop()


if __name__ == "__main__":
    sys.exit(main())
