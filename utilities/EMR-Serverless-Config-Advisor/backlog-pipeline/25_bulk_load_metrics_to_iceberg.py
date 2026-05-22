#!/usr/bin/env python3
"""
Bulk Load Metrics from S3 to Iceberg Tables (Most Recent 1 Hour) - DW Production
==================================================================================
Reads all JSON files from the MOST RECENT 1-hour window of timestamp folders
and bulk loads them to Iceberg tables in one batch.

Strategy: Sorts ALL folders by timestamp (newest first), then takes only folders
from the most recent 1-hour window. This ensures you always get the freshest data.

This script is optimized for high throughput and should be run AFTER the orchestrator
completes its job submissions.

Tables:
  - spark_metrics_task_stage_v5: Task and stage level metrics
  - spark_metrics_config_v5: Spark configuration details

Usage:
  python3 25_bulk_load_metrics_to_iceberg.py \
    --s3-bucket ${S3_BUCKET} \
    --lookback-hours 1

Features:
  - Sorts ALL folders by timestamp (newest first)
  - Takes only folders from the MOST RECENT N-hour window
  - Reads ALL JSON files in parallel from discovered folders
  - Bulk loads to Iceberg tables in batches (500 records per batch)
  - Uses PyIceberg for efficient Iceberg writes
  - Retry logic for concurrent write conflicts
  - Validates data before writing (skips records with missing job_id)

Environment Variables:
  - AWS_REGION: AWS region (default: us-east-1)
  - AWS_PROFILE: AWS CLI profile (default: None = use instance role)
  - ICEBERG_NAMESPACE: Iceberg namespace (default: your_catalog_namespace)
  - HMS_URI: Hive Metastore URI (default: thrift://your-hms-host:9083)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
import pandas as pd
import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.exceptions import CommitFailedException

# ============================================================================
# Configuration
# ============================================================================

# AWS Configuration
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
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

# Batch Configuration
BATCH_SIZE = 500  # Records per Iceberg write batch
MAX_WORKERS = 40  # Parallel S3 reads (64 core server)


# ============================================================================
# Helper Functions
# ============================================================================

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


# ============================================================================
# S3 Discovery Functions
# ============================================================================

def discover_timestamp_folders(s3_bucket, lookback_hours):
    """
    Discover the MOST RECENT timestamp folders in test-target-metrics/.

    Strategy:
    1. List ALL timestamp folders
    2. Sort by timestamp (newest first)
    3. Take only folders from the most recent N hours

    This ensures we get the freshest data, not old data from N hours ago.

    Returns list of folder paths (e.g., ['test-target-metrics/20260519T140000Z_abc12345/'])
    """
    print("=" * 80)
    print("DISCOVERING MOST RECENT TIMESTAMP FOLDERS")
    print("=" * 80)
    print(f"S3 Bucket: {s3_bucket}")
    print(f"Strategy: Get folders from MOST RECENT {lookback_hours} hour(s)")
    print(f"Supported formats:")
    print(f"  - batch_YYYYMMDDTHHMMSSz")
    print(f"  - YYYYMMDDTHHMMSSz_*")
    print("-" * 80)

    s3_client = get_s3_client(AWS_PROFILE)
    prefix = "test-target-metrics/"

    current_time = datetime.utcnow()
    print(f"Current time (UTC): {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # List ALL folders under test-target-metrics/
    paginator = s3_client.get_paginator('list_objects_v2')
    all_folders = []

    print("Scanning S3 for ALL timestamp folders...")
    for page in paginator.paginate(Bucket=s3_bucket, Prefix=prefix, Delimiter='/'):
        for common_prefix in page.get('CommonPrefixes', []):
            folder_path = common_prefix['Prefix']

            # Extract timestamp from folder name
            # Supported formats:
            #   - batch_20260518T171704Z
            #   - 20260519T140000Z_abc12345
            folder_name = folder_path.replace(prefix, '').rstrip('/')

            # Skip non-timestamp folders (like spark_config_extract, task_stage_summary)
            if not any(char.isdigit() for char in folder_name):
                continue

            timestamp_str = None

            # Try format: batch_YYYYMMDDTHHMMSSz
            if folder_name.startswith('batch_'):
                timestamp_str = folder_name.replace('batch_', '').split('_')[0]
            # Try format: YYYYMMDDTHHMMSSz_something
            elif '_' in folder_name:
                timestamp_str = folder_name.split('_')[0]
            # Try format: YYYYMMDDTHHMMSSz (no underscore)
            else:
                timestamp_str = folder_name

            if timestamp_str:
                try:
                    # Parse timestamp (format: YYYYMMDDTHHMMSSz)
                    folder_time = datetime.strptime(timestamp_str, "%Y%m%dT%H%M%SZ")

                    # Add to list with parsed timestamp
                    all_folders.append({
                        'path': folder_path,
                        'name': folder_name,
                        'timestamp': folder_time
                    })
                except ValueError:
                    print(f"  ⚠ Skipping invalid timestamp: {folder_name}")

    print(f"✓ Found {len(all_folders)} total timestamp folders")

    if not all_folders:
        print("⚠ No timestamp folders found in S3")
        print("=" * 80)
        return []

    # Sort by timestamp (NEWEST FIRST)
    all_folders.sort(key=lambda x: x['timestamp'], reverse=True)

    # Get the most recent folder time
    most_recent_time = all_folders[0]['timestamp']
    cutoff_time = most_recent_time - timedelta(hours=lookback_hours)

    print(f"\nMost recent folder: {all_folders[0]['name']}")
    print(f"  Timestamp: {most_recent_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Cutoff time: {cutoff_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Taking folders from MOST RECENT {lookback_hours} hour(s)")
    print()

    # Take only folders within the most recent N hours
    selected_folders = []
    print("Selected folders (newest first):")

    for folder in all_folders:
        if folder['timestamp'] >= cutoff_time:
            age_minutes = int((current_time - folder['timestamp']).total_seconds() / 60)
            selected_folders.append(folder['path'])
            print(f"  ✓ {folder['name']} ({age_minutes} minutes old)")
        else:
            # Stop once we go past the cutoff
            break

    print()
    print(f"✓ Selected {len(selected_folders)} folders from MOST RECENT {lookback_hours} hour(s)")
    print(f"  (Oldest selected: {all_folders[len(selected_folders)-1]['timestamp'].strftime('%Y-%m-%d %H:%M:%S')})")
    print("=" * 80)
    print()

    return selected_folders


def list_all_json_files(s3_bucket, timestamp_folders, subfolder):
    """
    List ALL JSON files in specified subfolder across all timestamp folders.

    Args:
        s3_bucket: S3 bucket name
        timestamp_folders: List of timestamp folder paths
        subfolder: Subfolder name ('task_stage_summary' or 'spark_config_extract')

    Returns:
        list: List of S3 keys (full paths to JSON files)
    """
    print(f"\nListing JSON files in {subfolder}/...")
    print(f"Scanning {len(timestamp_folders)} timestamp folders...")

    s3_client = get_s3_client(AWS_PROFILE)
    all_files = []

    for folder in timestamp_folders:
        prefix = f"{folder}{subfolder}/"

        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=s3_bucket, Prefix=prefix):
            if 'Contents' not in page:
                continue

            for obj in page['Contents']:
                key = obj['Key']
                if key.endswith('.json'):
                    all_files.append(key)

    print(f"✓ Found {len(all_files)} JSON files in {subfolder}/")
    return all_files


def read_json_from_s3_parallel(s3_bucket, keys, max_workers=20):
    """
    Read multiple JSON files from S3 in parallel.

    Returns:
        list: List of parsed JSON dicts
    """
    print(f"\nReading {len(keys)} JSON files in parallel (max_workers={max_workers})...")

    s3_client = get_s3_client(AWS_PROFILE)
    results = []
    errors = 0

    def read_single_file(key):
        try:
            response = s3_client.get_object(Bucket=s3_bucket, Key=key)
            content = response['Body'].read().decode('utf-8')
            return json.loads(content)
        except Exception as e:
            return None  # Skip errors

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(read_single_file, key): key for key in keys}

        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result:
                results.append(result)
            else:
                errors += 1

            if i % 100 == 0:
                print(f"  Progress: {i}/{len(keys)} files read...")

    print(f"✓ Successfully read {len(results)} files")
    if errors > 0:
        print(f"⚠ Skipped {errors} files due to read errors")

    return results


# ============================================================================
# Data Transformation Functions
# ============================================================================

def flatten_task_stage_for_iceberg(data: dict, hash_mapping: dict = None) -> dict:
    """Flatten task_stage_summary JSON for Iceberg table"""
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
    if hash_mapping and app_id:
        flat['app_id_hash'] = hash_mapping.get(app_id, "")
    else:
        flat['app_id_hash'] = ""

    flat['extraction_timestamp'] = data.get('extraction_timestamp')
    flat['event_count'] = to_int(data.get('event_count'))

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

    # Stage summary
    stage = data.get('stage_summary', {})
    flat['stage_total_stages'] = to_int(stage.get('total_stages'))
    flat['stage_completed_stages'] = to_int(stage.get('completed_stages'))
    flat['stage_failed_stages'] = to_int(stage.get('failed_stages'))
    flat['stage_skipped_stages'] = to_int(stage.get('skipped_stages'))
    flat['stage_success_rate_percent'] = to_float(stage.get('success_rate_percent'))

    # Executor summary
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

    # I/O summary
    io = data.get('io_summary', {})
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

    # Cost factor
    flat['total_cost_factor'] = to_float(data.get('total_cost_factor'))

    # Spill summary
    spill = data.get('spill_summary', {})
    flat['spill_total_memory_spilled_gb'] = to_float(spill.get('total_memory_spilled_gb'))
    flat['spill_total_disk_spilled_gb'] = to_float(spill.get('total_disk_spilled_gb'))
    flat['spill_tasks_with_memory_spill_percent'] = to_float(spill.get('tasks_with_memory_spill_percent'))
    flat['spill_tasks_with_disk_spill_percent'] = to_float(spill.get('tasks_with_disk_spill_percent'))

    # Summary metrics
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
    """Flatten spark_config_extract JSON for Iceberg table"""
    spark_config = data.get('spark_configuration', {})
    app_id = data.get('application_id')

    # Get hash from mapping if available
    app_id_hash = hash_mapping.get(app_id, "") if hash_mapping and app_id else ""

    # Helper to get config value or empty string
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


# ============================================================================
# Iceberg Write Functions
# ============================================================================

def write_to_iceberg_batch(table, records: list, table_name: str, valid_columns: set = None):
    """Write records to Iceberg table using PyArrow"""
    if not records:
        return

    try:
        df = pd.DataFrame(records)

        # Filter to only columns that exist in the table schema
        if valid_columns:
            extra_cols = set(df.columns) - valid_columns
            if extra_cols:
                df = df[[col for col in df.columns if col in valid_columns]]

        # Cast numeric columns for task_stage_summary
        if table_name == TASK_STAGE_TABLE:
            # Cast to int64 (long)
            int_columns = [
                'event_count', 'stage_completed_stages', 'stage_failed_stages',
                'stage_skipped_stages', 'stage_total_stages', 'task_total_tasks',
                'task_completed_tasks', 'task_failed_tasks', 'task_killed_tasks',
                'executor_total_executors', 'executor_active_executors'
            ]
            for col in int_columns:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype('int64')

            # Cast to float64 (double)
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

        # Convert to PyArrow Table
        arrow_table = pa.Table.from_pandas(df)

        # Make application_id non-nullable
        schema = arrow_table.schema
        new_fields = []
        for field in schema:
            if field.name == 'application_id':
                new_fields.append(pa.field(field.name, field.type, nullable=False))
            else:
                new_fields.append(field)

        new_schema = pa.schema(new_fields)
        arrow_table = arrow_table.cast(new_schema)

        # Append with retry logic
        append_with_retry(table, arrow_table, f"{ICEBERG_NAMESPACE}.{table_name}", max_retries=3, delay_seconds=180)
        print(f"  ✓ Wrote {len(records)} records to {table_name}")
    except Exception as e:
        print(f"  ✗ Error writing to {table_name}: {e}")
        import traceback
        traceback.print_exc()


# ============================================================================
# Main Processing Functions
# ============================================================================

def bulk_load_task_stage_metrics(catalog, s3_bucket, timestamp_folders, hash_mapping):
    """
    Bulk load task_stage_summary JSON files to Iceberg table.

    Returns:
        int: Number of records loaded
    """
    print("=" * 80)
    print("BULK LOADING: task_stage_summary → spark_metrics_task_stage_v5")
    print("=" * 80)

    # Load table schema
    try:
        table = catalog.load_table(f"{ICEBERG_NAMESPACE}.{TASK_STAGE_TABLE}")
        table_columns = set([field.name for field in table.schema().fields])
        print(f"✓ Table loaded: {len(table_columns)} columns")
    except Exception as e:
        print(f"✗ Error loading table: {e}")
        return 0

    # List all JSON files
    json_keys = list_all_json_files(s3_bucket, timestamp_folders, 'task_stage_summary')

    if not json_keys:
        print("⚠ No JSON files found")
        print("=" * 80)
        return 0

    # Read all JSON files in parallel
    all_data = read_json_from_s3_parallel(s3_bucket, json_keys, max_workers=MAX_WORKERS)

    # Flatten and validate
    print(f"\nFlattening {len(all_data)} records...")
    flattened_records = []
    skipped_count = 0

    for data in all_data:
        # Validate job_id exists
        job_id = data.get('job_id') or data.get('application_info', {}).get('job_id')
        if not job_id or job_id.strip() == "":
            skipped_count += 1
            continue

        flattened = flatten_task_stage_for_iceberg(data, hash_mapping)
        flattened_records.append(flattened)

    print(f"✓ Flattened {len(flattened_records)} valid records")
    if skipped_count > 0:
        print(f"⚠ Skipped {skipped_count} records (missing job_id)")

    # Write in batches
    print(f"\nWriting to Iceberg in batches of {BATCH_SIZE}...")
    total_written = 0

    for i in range(0, len(flattened_records), BATCH_SIZE):
        batch = flattened_records[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        total_batches = (len(flattened_records) + BATCH_SIZE - 1) // BATCH_SIZE

        print(f"\n  Batch {batch_num}/{total_batches}: Writing {len(batch)} records...")
        write_to_iceberg_batch(table, batch, TASK_STAGE_TABLE, table_columns)
        total_written += len(batch)

    print(f"\n✓ Successfully loaded {total_written:,} records to {TASK_STAGE_TABLE}")
    print("=" * 80)
    print()

    return total_written


def bulk_load_config_metrics(catalog, s3_bucket, timestamp_folders, hash_mapping):
    """
    Bulk load spark_config_extract JSON files to Iceberg table.

    Returns:
        int: Number of records loaded
    """
    print("=" * 80)
    print("BULK LOADING: spark_config_extract → spark_metrics_config_v5")
    print("=" * 80)

    # Load table schema
    try:
        table = catalog.load_table(f"{ICEBERG_NAMESPACE}.{CONFIG_TABLE}")
        table_columns = set([field.name for field in table.schema().fields])
        print(f"✓ Table loaded: {len(table_columns)} columns")
    except Exception as e:
        print(f"✗ Error loading table: {e}")
        return 0

    # List all JSON files
    json_keys = list_all_json_files(s3_bucket, timestamp_folders, 'spark_config_extract')

    if not json_keys:
        print("⚠ No JSON files found")
        print("=" * 80)
        return 0

    # Read all JSON files in parallel
    all_data = read_json_from_s3_parallel(s3_bucket, json_keys, max_workers=MAX_WORKERS)

    # Flatten and validate
    print(f"\nFlattening {len(all_data)} records...")
    flattened_records = []
    skipped_count = 0

    for data in all_data:
        # Validate job_id exists
        job_id = data.get('job_id')
        if not job_id or job_id.strip() == "":
            skipped_count += 1
            continue

        flattened = flatten_config_for_iceberg(data, hash_mapping)
        flattened_records.append(flattened)

    print(f"✓ Flattened {len(flattened_records)} valid records")
    if skipped_count > 0:
        print(f"⚠ Skipped {skipped_count} records (missing job_id)")

    # Write in batches
    print(f"\nWriting to Iceberg in batches of {BATCH_SIZE}...")
    total_written = 0

    for i in range(0, len(flattened_records), BATCH_SIZE):
        batch = flattened_records[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        total_batches = (len(flattened_records) + BATCH_SIZE - 1) // BATCH_SIZE

        print(f"\n  Batch {batch_num}/{total_batches}: Writing {len(batch)} records...")
        write_to_iceberg_batch(table, batch, CONFIG_TABLE, table_columns)
        total_written += len(batch)

    print(f"\n✓ Successfully loaded {total_written:,} records to {CONFIG_TABLE}")
    print("=" * 80)
    print()

    return total_written


# ============================================================================
# Main Execution
# ============================================================================

def main():
    """Main execution"""
    parser = argparse.ArgumentParser(
        description='Bulk load metrics from MOST RECENT timestamp folders to Iceberg tables',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Load from MOST RECENT 1 hour (DW Production)
  python3 25_bulk_load_metrics_to_iceberg.py --s3-bucket ${S3_BUCKET} --lookback-hours 1

  # Load from MOST RECENT 2 hours (DW Production)
  python3 25_bulk_load_metrics_to_iceberg.py --s3-bucket ${S3_BUCKET} --lookback-hours 2

Note: Script sorts ALL folders by timestamp (newest first), then takes only the most recent N-hour window.
        """
    )

    parser.add_argument('--s3-bucket', required=True, help='S3 bucket name')
    parser.add_argument('--lookback-hours', type=int, default=1,
                       help='Hours from MOST RECENT folder to include (default: 1)')

    args = parser.parse_args()

    start_time = datetime.now()

    print("\n" + "=" * 80)
    print("BULK METRICS LOADER (PyIceberg)")
    print("=" * 80)
    print(f"Start Time:      {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"S3 Bucket:       {args.s3_bucket}")
    print(f"Strategy:        Load from MOST RECENT {args.lookback_hours} hour(s)")
    print(f"Batch Size:      {BATCH_SIZE} records/batch")
    print(f"Max Workers:     {MAX_WORKERS} (parallel S3 reads)")
    print(f"Iceberg NS:      {ICEBERG_NAMESPACE}")
    print(f"HMS URI:         {HMS_URI}")
    print("=" * 80)
    print()

    try:
        # Initialize Iceberg catalog
        print("Initializing PyIceberg catalog...")
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

        # Step 1: Discover timestamp folders
        timestamp_folders = discover_timestamp_folders(args.s3_bucket, args.lookback_hours)

        if not timestamp_folders:
            print("⚠ No recent timestamp folders found. Exiting.")
            return 0

        # Step 2: Bulk load task_stage_summary
        task_stage_count = bulk_load_task_stage_metrics(
            catalog, args.s3_bucket, timestamp_folders, hash_mapping
        )

        # Step 3: Bulk load spark_config_extract
        config_count = bulk_load_config_metrics(
            catalog, args.s3_bucket, timestamp_folders, hash_mapping
        )

        # Summary
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        print("=" * 80)
        print("EXECUTION SUMMARY")
        print("=" * 80)
        print(f"Start Time:              {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"End Time:                {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Duration:                {duration:.1f} seconds ({duration/60:.1f} minutes)")
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
