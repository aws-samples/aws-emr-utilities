#!/usr/bin/env python3
"""
Load metrics from test-target-metrics timestamp folders to Iceberg tables.

Reads JSON files from recent 1 hour timestamp folders:
  - s3://.../test-target-metrics/{timestamp}/task_stage_summary/*.json → spark_metrics_task_stage_v5
  - s3://.../test-target-metrics/{timestamp}/spark_config_extract/*.json → spark_metrics_config_v5

Usage:
  python3 09_load_metrics_to_iceberg.py \
    --s3-bucket ${S3_BUCKET} \
    --lookback-hours 1
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
import boto3
import pandas as pd
import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.exceptions import CommitFailedException

# AWS Region
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# AWS Profile (None = use instance role)
AWS_PROFILE = os.environ.get('AWS_PROFILE') or None

# Iceberg Configuration - HMS via WaggleDance
ICEBERG_CATALOG_NAME = os.getenv("ICEBERG_CATALOG_NAME", "hive")
ICEBERG_NAMESPACE = os.getenv("ICEBERG_NAMESPACE", "your_catalog_namespace")
TASK_STAGE_TABLE = os.getenv("ICEBERG_TABLE_TASK_STAGE", "spark_metrics_task_stage_v5")
CONFIG_TABLE = os.getenv("ICEBERG_TABLE_CONFIG", "spark_metrics_config_v5")
BACKLOG_TABLE = os.getenv("ICEBERG_BACKLOG_TABLE", "backlog_events_log_v5")
HMS_URI = os.getenv("HMS_URI", "thrift://your-hms-host:9083")
HMS_USE_SSL = os.getenv("HMS_USE_SSL", "false").lower() == "true"

# STS Role for S3 access (optional)
ASSUME_ROLE_ARN = os.getenv("ASSUME_ROLE_ARN", "")

# Batch size for writing to Iceberg
BATCH_SIZE = 500


def append_with_retry(iceberg_table, arrow_table, table_name, max_retries=3, delay_seconds=180):
    """
    Append to Iceberg table with retry logic for concurrency conflicts.

    Args:
        iceberg_table: PyIceberg table instance
        arrow_table: PyArrow table to append
        table_name: Table name for logging
        max_retries: Maximum number of retry attempts (default: 3)
        delay_seconds: Delay between retries in seconds (default: 180 = 3 minutes)

    Raises:
        CommitFailedException: If all retries fail or non-conflict error occurs
    """
    for attempt in range(max_retries):
        try:
            iceberg_table.append(arrow_table)
            if attempt > 0:
                print(f"  ✓ Write succeeded on attempt {attempt + 1}")
            return
        except CommitFailedException as e:
            error_msg = str(e)
            is_conflict = "branch main has changed" in error_msg or "expected id" in error_msg

            if is_conflict and attempt < max_retries - 1:
                print(f"\n  ⚠️  CONCURRENCY CONFLICT (Attempt {attempt + 1}/{max_retries})")
                print(f"     Table: {table_name}")
                print(f"     Error: {error_msg}")
                print(f"     Retrying in {delay_seconds}s ({delay_seconds//60}m)...")
                time.sleep(delay_seconds)
                iceberg_table.refresh()  # Refresh to get latest snapshot
                print(f"  ✓ Table refreshed, retrying...")
            else:
                if is_conflict:
                    print(f"\n  ❌ WRITE FAILED AFTER {max_retries} ATTEMPTS")
                    print(f"     Table: {table_name}")
                raise


def get_s3_client(profile=None):
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client('s3', region_name=AWS_REGION)


def get_s3_credentials(profile=None, role_arn=None):
    """Get S3 credentials for PyIceberg - tries STS assume-role first, falls back to profile credentials"""
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()

    # Try STS assume-role first
    if role_arn:
        try:
            sts = session.client('sts')
            response = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName="pyiceberg-hms-session",
                DurationSeconds=3600
            )
            creds = response['Credentials']
            print(f"✓ Assumed role: {role_arn}")
            print(f"  Session expires: {creds['Expiration']}")
            return {
                "s3.access-key-id": creds['AccessKeyId'],
                "s3.secret-access-key": creds['SecretAccessKey'],
                "s3.session-token": creds['SessionToken'],
            }
        except Exception as e:
            print(f"  ⚠ Could not assume role {role_arn}: {e}")
            print(f"  Falling back to profile credentials...")

    # Fallback: use profile's own credentials
    credentials = session.get_credentials()
    if credentials:
        frozen = credentials.get_frozen_credentials()
        cred_dict = {
            "s3.access-key-id": frozen.access_key,
            "s3.secret-access-key": frozen.secret_key,
        }
        if frozen.token:
            cred_dict["s3.session-token"] = frozen.token
        print(f"✓ Using profile credentials: {profile or 'default'}")
        return cred_dict

    print("  ⚠ No credentials found — PyIceberg will use default credential chain")
    return {}


def list_s3_files(bucket: str, prefix: str, profile=None):
    """List all JSON files in S3 prefix"""
    s3_client = get_s3_client(profile)
    files = []
    paginator = s3_client.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
    for page in pages:
        if 'Contents' in page:
            for obj in page['Contents']:
                key = obj['Key']
                if key.endswith('.json') and not key.endswith('/'):
                    files.append(key)
    return files


def read_json_from_s3(bucket: str, key: str, profile=None):
    """Read and parse JSON file from S3"""
    s3_client = get_s3_client(profile)
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read().decode('utf-8')
        return json.loads(content)
    except Exception as e:
        print(f"Error reading {key}: {e}")
        return None


def load_hash_mapping_from_backlog(catalog, backlog_table_name):
    """
    Load app_id_hash mapping from backlog table.
    Returns dict: {application_id: app_id_hash}
    """
    try:
        print(f"\nLoading app_id_hash from {backlog_table_name}...")
        table = catalog.load_table(backlog_table_name)
        scan = table.scan()

        hash_mapping = {}
        for batch in scan.to_arrow():
            df = batch.to_pandas()
            # Handle case where df might be a Series (single column) or DataFrame
            if hasattr(df, 'iterrows'):
                for _, row in df.iterrows():
                    app_id = row.get('application_id') if isinstance(row, dict) else getattr(row, 'application_id', None)
                    app_hash = row.get('app_id_hash') if isinstance(row, dict) else getattr(row, 'app_id_hash', None)
                    if app_id and app_hash:
                        hash_mapping[app_id] = app_hash
            elif len(df) > 0:
                # Single row case
                app_id = df.get('application_id') if hasattr(df, 'get') else None
                app_hash = df.get('app_id_hash') if hasattr(df, 'get') else None
                if app_id and app_hash:
                    hash_mapping[app_id] = app_hash

        print(f"✓ Loaded {len(hash_mapping)} app_id_hash mappings from backlog table")
        return hash_mapping
    except Exception as e:
        print(f"⚠ Warning: Could not load hash mapping from backlog table: {e}")
        print(f"  Will proceed without app_id_hash values")
        return {}


def list_timestamp_folders(s3_bucket, lookback_hours):
    """
    List all timestamp folders in test-target-metrics/ created within lookback hours.

    Args:
        s3_bucket: S3 bucket name
        lookback_hours: Number of hours to look back

    Returns:
        list: List of timestamp folder paths that match the time window
    """
    print("=" * 80)
    print("DISCOVERING RECENT TIMESTAMP FOLDERS")
    print("=" * 80)
    print(f"S3 Bucket: {s3_bucket}")
    print(f"Lookback: Last {lookback_hours} hour(s)")
    print("-" * 80)

    s3_client = get_s3_client(AWS_PROFILE)
    prefix = "test-target-metrics/"

    # Calculate cutoff time
    cutoff_time = datetime.utcnow() - timedelta(hours=lookback_hours)
    print(f"Cutoff time: {cutoff_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Looking for folders: {prefix}YYYYMMDDTHHMMSSz_*/")
    print()

    # List all folders under test-target-metrics/
    paginator = s3_client.get_paginator('list_objects_v2')
    timestamp_folders = []

    print("Scanning S3 for timestamp folders...")
    for page in paginator.paginate(Bucket=s3_bucket, Prefix=prefix, Delimiter='/'):
        for common_prefix in page.get('CommonPrefixes', []):
            folder_path = common_prefix['Prefix']

            # Extract timestamp from folder name
            # Format: test-target-metrics/20260519T140000Z_abc12345/
            folder_name = folder_path.replace(prefix, '').rstrip('/')

            if '_' in folder_name:
                timestamp_str = folder_name.split('_')[0]  # e.g., 20260519T140000Z

                try:
                    # Parse timestamp (format: YYYYMMDDTHHMMSSz)
                    folder_time = datetime.strptime(timestamp_str, "%Y%m%dT%H%M%SZ")

                    # Check if within lookback window
                    if folder_time >= cutoff_time:
                        timestamp_folders.append(folder_path)
                        print(f"  ✓ {folder_name} ({folder_time.strftime('%Y-%m-%d %H:%M:%S UTC')})")
                except ValueError:
                    print(f"  ⚠ Skipping invalid timestamp: {folder_name}")

    print()
    print(f"✓ Found {len(timestamp_folders)} timestamp folders within last {lookback_hours} hour(s)")
    print("=" * 80)
    print()

    return timestamp_folders


def flatten_enhanced_for_iceberg(data: dict, hash_mapping: dict = None) -> dict:
    """Flatten enhanced nested dictionary for Iceberg table with proper type casting

    Args:
        data: Metrics data dictionary
        hash_mapping: Optional dict mapping application_id to app_id_hash
    """
    flat = {}

    # Helper to safely convert to float
    def to_float(val):
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    # Helper to safely convert to int (for LONG columns)
    def to_int(val):
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    # Top-level fields
    app_id = data.get('application_id')
    flat['application_id'] = app_id

    # Add app_id_hash from backlog table if available
    # Use empty string instead of None to avoid null type in Iceberg v2
    if hash_mapping and app_id:
        flat['app_id_hash'] = hash_mapping.get(app_id, "")
    else:
        flat['app_id_hash'] = ""

    flat['extraction_timestamp'] = data.get('extraction_timestamp')
    flat['event_count'] = to_int(data.get('event_count'))  # LONG type

    # Application info
    app_info = data.get('application_info', {})
    flat['job_id'] = app_info.get('job_id')
    flat['cluster_id'] = app_info.get('cluster_id')
    flat['application_name'] = app_info.get('application_name')
    flat['app_id'] = app_info.get('app_id')
    flat['application_start_time'] = app_info.get('application_start_time')
    flat['application_end_time'] = app_info.get('application_end_time')
    flat['total_run_duration_minutes'] = to_float(app_info.get('total_run_duration_minutes'))
    flat['total_run_duration_hours'] = to_float(app_info.get('total_run_duration_hours'))

    # Task summary
    task = data.get('task_summary', {})
    flat['task_total_tasks'] = task.get('total_tasks')
    flat['task_completed_tasks'] = task.get('completed_tasks')
    flat['task_failed_tasks'] = task.get('failed_tasks')
    flat['task_killed_tasks'] = task.get('killed_tasks')
    flat['task_success_rate_percent'] = to_float(task.get('success_rate_percent'))

    # Stage summary - convert counts to LONG (int64)
    stage = data.get('stage_summary', {})
    flat['stage_total_stages'] = to_int(stage.get('total_stages'))  # LONG type
    flat['stage_completed_stages'] = to_int(stage.get('completed_stages'))  # LONG type
    flat['stage_failed_stages'] = to_int(stage.get('failed_stages'))  # LONG type
    flat['stage_skipped_stages'] = to_int(stage.get('skipped_stages'))  # LONG type
    flat['stage_success_rate_percent'] = to_float(stage.get('success_rate_percent'))

    # Executor summary - ensure all are floats
    executor = data.get('executor_summary', {})
    flat['executor_total_executors'] = executor.get('total_executors')
    flat['executor_active_executors'] = executor.get('active_executors')
    flat['executor_avg_memory_utilization_percent'] = to_float(executor.get('avg_memory_utilization_percent'))
    flat['executor_min_memory_utilization_percent'] = to_float(executor.get('min_memory_utilization_percent'))
    flat['executor_max_memory_utilization_percent'] = to_float(executor.get('max_memory_utilization_percent'))
    flat['executor_median_memory_utilization_percent'] = to_float(executor.get('median_memory_utilization_percent'))
    flat['executor_avg_cpu_utilization_percent'] = to_float(executor.get('avg_cpu_utilization_percent'))
    flat['executor_min_cpu_utilization_percent'] = to_float(executor.get('min_cpu_utilization_percent'))
    flat['executor_max_cpu_utilization_percent'] = to_float(executor.get('max_cpu_utilization_percent'))
    flat['executor_median_cpu_utilization_percent'] = to_float(executor.get('median_cpu_utilization_percent'))
    flat['executor_total_cost_factor'] = to_float(executor.get('total_cost_factor'))
    flat['executor_details'] = json.dumps(executor.get('executor_details', []))

    # I/O summary - ensure all are floats
    io = data.get('io_summary', {})
    # Handle nested structure - io_summary might have application_level
    if 'application_level' in io:
        io = io['application_level']

    flat['io_total_input_gb'] = to_float(io.get('total_input_gb'))
    flat['io_total_output_gb'] = to_float(io.get('total_output_gb'))
    flat['io_total_shuffle_read_gb'] = to_float(io.get('total_shuffle_read_gb'))
    flat['io_total_shuffle_write_gb'] = to_float(io.get('total_shuffle_write_gb'))
    flat['io_input_per_task_min_gb'] = to_float(io.get('input_per_task_min_gb'))
    flat['io_input_per_task_max_gb'] = to_float(io.get('input_per_task_max_gb'))
    flat['io_input_per_task_avg_gb'] = to_float(io.get('input_per_task_avg_gb'))
    flat['io_input_per_task_median_gb'] = to_float(io.get('input_per_task_median_gb'))
    flat['io_shuffle_read_per_task_min_gb'] = to_float(io.get('shuffle_read_per_task_min_gb'))
    flat['io_shuffle_read_per_task_max_gb'] = to_float(io.get('shuffle_read_per_task_max_gb'))
    flat['io_shuffle_read_per_task_avg_gb'] = to_float(io.get('shuffle_read_per_task_avg_gb'))
    flat['io_shuffle_read_per_task_median_gb'] = to_float(io.get('shuffle_read_per_task_median_gb'))
    flat['io_shuffle_write_per_task_min_gb'] = to_float(io.get('shuffle_write_per_task_min_gb'))
    flat['io_shuffle_write_per_task_max_gb'] = to_float(io.get('shuffle_write_per_task_max_gb'))
    flat['io_shuffle_write_per_task_avg_gb'] = to_float(io.get('shuffle_write_per_task_avg_gb'))
    flat['io_shuffle_write_per_task_median_gb'] = to_float(io.get('shuffle_write_per_task_median_gb'))

    # Top-level cost
    flat['total_cost_factor'] = to_float(data.get('total_cost_factor'))

    # Spill Summary - Always include these fields
    spill = data.get('spill_summary', {})
    flat['spill_total_memory_spilled_gb'] = to_float(spill.get('total_memory_spilled_gb'))
    flat['spill_total_disk_spilled_gb'] = to_float(spill.get('total_disk_spilled_gb'))
    flat['spill_tasks_with_memory_spill_percent'] = to_float(spill.get('tasks_with_memory_spill_percent'))
    flat['spill_tasks_with_disk_spill_percent'] = to_float(spill.get('tasks_with_disk_spill_percent'))

    # Summary metrics from enhanced categories - Cast to float (double) for table compatibility
    job_details = data.get('job_details', {})
    job_summary = job_details.get('summary', {}) if isinstance(job_details, dict) else {}
    flat['job_total_jobs'] = to_float(job_summary.get('total_jobs'))
    flat['job_successful_jobs'] = to_float(job_summary.get('successful_jobs'))
    flat['job_failed_jobs'] = to_float(job_summary.get('failed_jobs'))

    sql_metrics = data.get('sql_metrics', {})
    flat['sql_total_executions'] = to_float(sql_metrics.get('total_sql_executions')) if isinstance(sql_metrics, dict) else None

    driver_metrics = data.get('driver_metrics', {})
    flat['driver_total_tasks_launched'] = to_float(driver_metrics.get('total_tasks_launched')) if isinstance(driver_metrics, dict) else None
    flat['driver_memory_utilization_percent'] = to_float(driver_metrics.get('memory_utilization_percent')) if isinstance(driver_metrics, dict) else None

    return flat


def flatten_config_for_iceberg(data: dict, hash_mapping: dict = None) -> dict:
    """Flatten config data for Iceberg with additional config fields

    Args:
        data: Config data dictionary
        hash_mapping: Optional dict mapping application_id to app_id_hash
    """
    spark_config = data.get('spark_configuration', {})
    app_id = data.get('application_id')

    # Get hash from mapping if available
    # Use empty string instead of None to avoid null type in Iceberg v2
    app_id_hash = hash_mapping.get(app_id, "") if hash_mapping and app_id else ""

    # Helper to get config value or empty string (avoid null type in Iceberg v2)
    def get_config_or_empty(key):
        val = spark_config.get(key)
        return val if val is not None else ""

    return {
        'application_id': app_id,
        'app_id_hash': app_id_hash,
        'extraction_timestamp': data.get('extraction_timestamp'),
        'cluster_id': data.get('cluster_id'),
        'job_id': data.get('job_id'),
        'application_name': data.get('application_name'),
        'app_id': data.get('app_id'),
        'application_start_time': data.get('application_start_time'),
        'application_end_time': data.get('application_end_time'),
        'total_run_duration_minutes': data.get('total_run_duration_minutes'),
        'total_run_duration_hours': data.get('total_run_duration_hours'),
        'total_cost_factor': data.get('total_cost_factor'),
        'spark_executor_cores': get_config_or_empty('spark.executor.cores'),
        'spark_executor_memory': get_config_or_empty('spark.executor.memory'),
        'spark_executor_instances': get_config_or_empty('spark.executor.instances'),
        'spark_dynamic_allocation_enabled': get_config_or_empty('spark.dynamicAllocation.enabled'),
        'spark_sql_shuffle_partitions': get_config_or_empty('spark.sql.shuffle.partitions'),
        'spark_sql_files_max_partition_bytes': get_config_or_empty('spark.sql.files.maxPartitionBytes'),
        'spark_driver_cores': get_config_or_empty('spark.driver.cores'),
        'spark_driver_memory': get_config_or_empty('spark.driver.memory'),
        'spark_dynamic_allocation_min_executors': get_config_or_empty('spark.dynamicAllocation.minExecutors'),
        'spark_dynamic_allocation_max_executors': get_config_or_empty('spark.dynamicAllocation.maxExecutors'),
    }


def load_task_stage_metrics(catalog, s3_bucket, timestamp_folders, hash_mapping):
    """
    Load task_stage_summary JSON files to Iceberg table using PyIceberg.

    Args:
        catalog: PyIceberg catalog instance
        s3_bucket: S3 bucket name
        timestamp_folders: List of timestamp folder paths
        hash_mapping: Dict mapping application_id to app_id_hash

    Returns:
        int: Number of records loaded
    """
    print("=" * 80)
    print("LOADING: task_stage_summary → spark_metrics_task_stage_v5")
    print("=" * 80)

    # Build list of S3 paths to read
    s3_paths = []
    for folder in timestamp_folders:
        # Construct path to task_stage_summary subfolder
        path = f"{folder}task_stage_summary/"
        s3_paths.append(path)
        print(f"  Reading: s3://{s3_bucket}/{path}")

    print()

    if not s3_paths:
        print("⚠ No paths to read")
        print("=" * 80)
        return 0

    # Load table and get valid columns
    try:
        table = catalog.load_table(f"{ICEBERG_NAMESPACE}.{TASK_STAGE_TABLE}")
        table_columns = set([field.name for field in table.schema().fields])
        print(f"✓ Table loaded: {len(table_columns)} columns")
    except Exception as e:
        print(f"✗ Error loading table: {e}")
        return 0

    # Collect all JSON files
    all_files = []
    for prefix in s3_paths:
        files = list_s3_files(s3_bucket, prefix, AWS_PROFILE)
        all_files.extend(files)

    print(f"\nFound {len(all_files)} JSON files")

    if not all_files:
        print("⚠ No JSON files found")
        print("=" * 80)
        return 0

    # Process in batches
    batch = []
    total_written = 0
    skipped_count = 0

    for i, key in enumerate(all_files):
        data = read_json_from_s3(s3_bucket, key, AWS_PROFILE)
        if data:
            # Validate job_id exists
            job_id = data.get('job_id') or data.get('application_info', {}).get('job_id')
            if not job_id or job_id.strip() == "":
                skipped_count += 1
                continue

            flattened = flatten_enhanced_for_iceberg(data, hash_mapping)
            batch.append(flattened)

            # Write batch
            if len(batch) >= BATCH_SIZE:
                print(f"\n  Writing batch of {len(batch)} records...")
                write_to_iceberg_pandas(table, batch, TASK_STAGE_TABLE, table_columns)
                total_written += len(batch)
                batch.clear()

        if (i + 1) % 50 == 0:
            print(f"  Processed {i + 1}/{len(all_files)} files...")

    # Write remaining
    if batch:
        print(f"\n  Writing final batch of {len(batch)} records...")
        write_to_iceberg_pandas(table, batch, TASK_STAGE_TABLE, table_columns)
        total_written += len(batch)

    if skipped_count > 0:
        print(f"\n  ⚠ Skipped {skipped_count} files (missing job_id)")

    print(f"\n✓ Successfully loaded {total_written:,} records to {TASK_STAGE_TABLE}")
    print("=" * 80)
    print()

    return total_written


def write_to_iceberg_pandas(table, records: list, table_name: str, valid_columns: set = None):
    """Write records to Iceberg table using pandas + PyArrow, filtering to only valid columns"""
    if not records:
        return

    try:
        df = pd.DataFrame(records)

        # Filter to only columns that exist in the table schema
        if valid_columns:
            extra_cols = set(df.columns) - valid_columns
            if extra_cols:
                print(f"  ⚠ Dropping {len(extra_cols)} columns not in table: {', '.join(sorted(extra_cols)[:5])}{'...' if len(extra_cols) > 5 else ''}")
                df = df[[col for col in df.columns if col in valid_columns]]

        # Explicitly cast numeric columns for task_stage_summary
        if table_name == TASK_STAGE_TABLE:
            # Cast these fields to int64 (long) - they represent counts
            int_columns = [
                'event_count',
                'stage_completed_stages',
                'stage_failed_stages',
                'stage_skipped_stages',
                'stage_total_stages',
                'task_total_tasks',
                'task_completed_tasks',
                'task_failed_tasks',
                'task_killed_tasks',
                'executor_total_executors',
                'executor_active_executors'
            ]
            for col in int_columns:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype('int64')

            # Cast these fields to float64 (double)
            float_columns = [
                'total_run_duration_minutes', 'total_run_duration_hours',
                'task_success_rate_percent', 'stage_success_rate_percent',
                'executor_avg_memory_utilization_percent', 'executor_min_memory_utilization_percent',
                'executor_max_memory_utilization_percent', 'executor_median_memory_utilization_percent',
                'executor_avg_cpu_utilization_percent', 'executor_min_cpu_utilization_percent',
                'executor_max_cpu_utilization_percent', 'executor_median_cpu_utilization_percent',
                'executor_total_cost_factor',
                'io_total_input_gb', 'io_total_output_gb', 'io_total_shuffle_read_gb', 'io_total_shuffle_write_gb',
                'io_input_per_task_min_gb', 'io_input_per_task_max_gb', 'io_input_per_task_avg_gb',
                'io_input_per_task_median_gb', 'io_shuffle_read_per_task_min_gb', 'io_shuffle_read_per_task_max_gb',
                'io_shuffle_read_per_task_avg_gb', 'io_shuffle_read_per_task_median_gb',
                'io_shuffle_write_per_task_min_gb', 'io_shuffle_write_per_task_max_gb',
                'io_shuffle_write_per_task_avg_gb', 'io_shuffle_write_per_task_median_gb',
                'total_cost_factor', 'driver_memory_utilization_percent',
                'spill_total_memory_spilled_gb', 'spill_total_disk_spilled_gb',
                'spill_tasks_with_memory_spill_percent', 'spill_tasks_with_disk_spill_percent',
                'job_total_jobs', 'job_successful_jobs', 'job_failed_jobs',
                'sql_total_executions', 'driver_total_tasks_launched'
            ]
            for col in float_columns:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').astype('float64')

        # Convert pandas DataFrame to PyArrow Table
        arrow_table = pa.Table.from_pandas(df)

        # Make application_id non-nullable to match Iceberg schema
        schema = arrow_table.schema
        new_fields = []
        for field in schema:
            if field.name == 'application_id':
                new_fields.append(pa.field(field.name, field.type, nullable=False))
            else:
                new_fields.append(field)

        new_schema = pa.schema(new_fields)
        arrow_table = arrow_table.cast(new_schema)

        # Append with retry logic for concurrency conflicts
        append_with_retry(table, arrow_table, f"{ICEBERG_NAMESPACE}.{table_name}", max_retries=3, delay_seconds=180)
        print(f"  ✓ Wrote {len(records)} records to {table_name}")
    except Exception as e:
        print(f"  ✗ Error writing to {table_name}: {e}")
        import traceback
        traceback.print_exc()


def load_config_metrics(catalog, s3_bucket, timestamp_folders, hash_mapping):
    """
    Load spark_config_extract JSON files to Iceberg table using PyIceberg.

    Args:
        catalog: PyIceberg catalog instance
        s3_bucket: S3 bucket name
        timestamp_folders: List of timestamp folder paths
        hash_mapping: Dict mapping application_id to app_id_hash

    Returns:
        int: Number of records loaded
    """
    print("=" * 80)
    print("LOADING: spark_config_extract → spark_metrics_config_v5")
    print("=" * 80)

    # Build list of S3 paths to read
    s3_paths = []
    for folder in timestamp_folders:
        # Construct path to spark_config_extract subfolder
        path = f"{folder}spark_config_extract/"
        s3_paths.append(path)
        print(f"  Reading: s3://{s3_bucket}/{path}")

    print()

    if not s3_paths:
        print("⚠ No paths to read")
        print("=" * 80)
        return 0

    # Load table and get valid columns
    try:
        table = catalog.load_table(f"{ICEBERG_NAMESPACE}.{CONFIG_TABLE}")
        table_columns = set([field.name for field in table.schema().fields])
        print(f"✓ Table loaded: {len(table_columns)} columns")
    except Exception as e:
        print(f"✗ Error loading table: {e}")
        return 0

    # Collect all JSON files
    all_files = []
    for prefix in s3_paths:
        files = list_s3_files(s3_bucket, prefix, AWS_PROFILE)
        all_files.extend(files)

    print(f"\nFound {len(all_files)} JSON files")

    if not all_files:
        print("⚠ No JSON files found")
        print("=" * 80)
        return 0

    # Process in batches
    batch = []
    total_written = 0
    skipped_count = 0

    for i, key in enumerate(all_files):
        data = read_json_from_s3(s3_bucket, key, AWS_PROFILE)
        if data:
            # Validate job_id exists
            job_id = data.get('job_id') or data.get('application_info', {}).get('job_id')
            if not job_id or job_id.strip() == "":
                skipped_count += 1
                continue

            flattened = flatten_config_for_iceberg(data, hash_mapping)
            batch.append(flattened)

            # Write batch
            if len(batch) >= BATCH_SIZE:
                print(f"\n  Writing batch of {len(batch)} records...")
                write_to_iceberg_pandas(table, batch, CONFIG_TABLE, table_columns)
                total_written += len(batch)
                batch.clear()

        if (i + 1) % 50 == 0:
            print(f"  Processed {i + 1}/{len(all_files)} files...")

    # Write remaining
    if batch:
        print(f"\n  Writing final batch of {len(batch)} records...")
        write_to_iceberg_pandas(table, batch, CONFIG_TABLE, table_columns)
        total_written += len(batch)

    if skipped_count > 0:
        print(f"\n  ⚠ Skipped {skipped_count} files (missing job_id)")

    print(f"\n✓ Successfully loaded {total_written:,} records to {CONFIG_TABLE}")
    print("=" * 80)
    print()

    return total_written


def main():
    """Main execution."""
    parser = argparse.ArgumentParser(description='Load metrics from test-target-metrics to Iceberg tables')
    parser.add_argument('--s3-bucket', required=True, help='S3 bucket name')
    parser.add_argument('--lookback-hours', type=int, default=1, help='Hours to look back (default: 1)')
    parser.add_argument('--iceberg-warehouse', default='s3://${S3_BUCKET}/iceberg/', help='Iceberg warehouse location (unused, for compatibility)')

    args = parser.parse_args()

    start_time = datetime.now()

    print("\n" + "=" * 80)
    print("METRICS TO ICEBERG LOADER (PyIceberg)")
    print("=" * 80)
    print(f"Start Time: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"S3 Bucket: {args.s3_bucket}")
    print(f"Lookback: {args.lookback_hours} hour(s)")
    print("=" * 80)
    print()

    # Initialize Iceberg catalog (HMS via WaggleDance)
    print("Initializing Iceberg catalog (HMS via WaggleDance)...")
    print(f"  HMS URI: {HMS_URI}")

    # Get S3 credentials (tries assume-role, falls back to profile)
    s3_creds = get_s3_credentials(profile=AWS_PROFILE, role_arn=ASSUME_ROLE_ARN)

    catalog = load_catalog(
        ICEBERG_CATALOG_NAME,
        **{
            "type": "hive",
            "uri": HMS_URI,
            "hive.metastore.use-ssl": str(HMS_USE_SSL).lower(),
            **s3_creds,
        }
    )
    print("✓ Catalog ready\n")

    # Load hash mapping from backlog table
    hash_mapping = load_hash_mapping_from_backlog(catalog, f"{ICEBERG_NAMESPACE}.{BACKLOG_TABLE}")

    try:
        # Step 1: Discover recent timestamp folders
        timestamp_folders = list_timestamp_folders(args.s3_bucket, args.lookback_hours)

        if not timestamp_folders:
            print("⚠ No recent timestamp folders found. Exiting.")
            return 0

        # Step 2: Load task_stage_summary
        task_stage_count = load_task_stage_metrics(catalog, args.s3_bucket, timestamp_folders, hash_mapping)

        # Step 3: Load spark_config_extract
        config_count = load_config_metrics(catalog, args.s3_bucket, timestamp_folders, hash_mapping)

        # Summary
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        print("=" * 80)
        print("EXECUTION SUMMARY")
        print("=" * 80)
        print(f"Start Time:              {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"End Time:                {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Duration:                {duration:.1f} seconds")
        print(f"Timestamp Folders:       {len(timestamp_folders)}")
        print(f"Task/Stage Records:      {task_stage_count:,}")
        print(f"Config Records:          {config_count:,}")
        print(f"Total Records Loaded:    {task_stage_count + config_count:,}")
        print("=" * 80)

        return 0

    except Exception as e:
        print(f"\n✗ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
