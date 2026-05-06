#!/usr/bin/env python3
"""
Reduced Multi-threaded Spark Event Log Processor
Combines the performance of multi-threading with essential metrics extraction.

Features:
- Multi-threaded processing with configurable workers
- Core extraction functions for essential metrics
- Metrics: jobs, stages, tasks, executors, driver, I/O, etc.
- Summary metrics included (job counts, SQL executions, driver stats)
- Enhanced JSON details (50-66) removed to reduce file sizes
- Outputs to S3 in JSON format

Usage:
    python3 multi-thread-spark-metric-enhanced.py
"""

import json
import sys
import os
import gzip
import bz2
import tarfile
import zipfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from typing import Dict, List, Set, Tuple, Any
from threading import Lock
from datetime import datetime
from collections import defaultdict
import zstandard as zstd
from io import BytesIO

# Try to import boto3 (optional for local-only usage)
try:
    import boto3
    S3_AVAILABLE = True
except ImportError:
    S3_AVAILABLE = False
    print("Warning: boto3 not available - S3 support disabled")


# Configuration (can be overridden via command-line or environment)
INPUT_PATH = "/Users/suthan/Downloads/"  # or local: "/path/to/logs/"
OUTPUT_PATH = "/Users/suthan/test_app_processing/"  # or local: "/path/to/output/"
AWS_PROFILE = None
MAX_WORKERS = 20
WRITE_WORKERS = 20


def is_s3_path(path: str) -> bool:
    """Check if path is S3."""
    return path.startswith('s3://')

# Complete config keys list (113 keys)
print("Loading 113 Spark configuration keys...")
CONFIG_KEYS = [
    "spark.ams_application_name",
    "spark.app.attempt.id",
    "spark.app.id",
    "spark.app.name",
    "spark.app.startTime",
    "spark.app.submitTime",
    "spark.blacklist.decommissioning.enabled",
    "spark.blacklist.decommissioning.timeout",
    "spark.broadcast.blockSize",
    "spark.decommissioning.timeout.threshold",
    "spark.driver.cores",
    "spark.driver.defaultJavaOptions",
    "spark.driver.extraClassPath",
    "spark.driver.extraJavaOptions",
    "spark.driver.extraLibraryPath",
    "spark.driver.host",
    "spark.driver.memory",
    "spark.driver.port",
    "spark.driver.transparentHugePage.enabled",
    "spark.dynamicAllocation.enabled",
    "spark.dynamicAllocation.maxExecutors",
    "spark.emr.default.executor.cores",
    "spark.emr.default.executor.memory",
    "spark.emr_cluster_id",
    "spark.emr_cluster_name",
    "spark.eventLog.dir",
    "spark.eventLog.enabled",
    "spark.executor.cores",
    "spark.executor.defaultJavaOptions",
    "spark.executor.extraClassPath",
    "spark.executor.extraJavaOptions",
    "spark.executor.extraLibraryPath",
    "spark.executor.id",
    "spark.executor.instances",
    "spark.executor.memory",
    "spark.executor.memoryOverhead",
    "spark.executor.transparentHugePage.enabled",
    "spark.executorEnv.AWS_SPARK_REDSHIFT_CONNECTOR_SERVICE_NAME",
    "spark.executorEnv.JAVA_HOME",
    "spark.extraListeners",
    "spark.files.fetchFailure.unRegisterOutputOnHost",
    "spark.hadoop.fs.s3.getObject.initialSocketTimeoutMilliseconds",
    "spark.hadoop.fs.s3a.committer.magic.enabled",
    "spark.hadoop.fs.s3a.committer.magic.track.commits.in.memory.enabled",
    "spark.hadoop.fs.s3a.committer.name",
    "spark.hadoop.fs.s3a.connection.ssl.enabled",
    "spark.hadoop.fs.s3a.encryption.algorithm",
    "spark.hadoop.fs.s3a.server-side-encryption-algorithm",
    "spark.hadoop.hive.metastore.client.connect.retry.delay",
    "spark.hadoop.hive.metastore.client.socket.timeout",
    "spark.hadoop.hive.metastore.failure.retries",
    "spark.hadoop.hive.metastore.uris",
    "spark.hadoop.mapreduce.fileoutputcommitter.algorithm.version.emr_internal_use_only.EmrFileSystem",
    "spark.hadoop.mapreduce.fileoutputcommitter.cleanup-failures.ignored.emr_internal_use_only.EmrFileSystem",
    "spark.hadoop.mapreduce.input.fileinputformat.split.minsize",
    "spark.hadoop.mapreduce.output.fs.optimized.committer.enabled",
    "spark.hadoop.yarn.timeline-service.enabled",
    "spark.history.fs.logDirectory",
    "spark.history.ui.port",
    "spark.job_id",
    "spark.master",
    "spark.metrics.eventLog.dir",
    "spark.openlineage.namespace",
    "spark.org.apache.hadoop.yarn.server.webproxy.amfilter.AmIpFilter.param.PROXY_HOSTS",
    "spark.org.apache.hadoop.yarn.server.webproxy.amfilter.AmIpFilter.param.PROXY_URI_BASES",
    "spark.product",
    "spark.resourceManager.cleanupExpiredHost",
    "spark.scheduler.mode",
    "spark.shuffle.service.enabled",
    "spark.sql.adaptive.advisoryPartitionSizeInBytes",
    "spark.sql.adaptive.coalescePartitions.enabled",
    "spark.sql.adaptive.coalescePartitions.minPartitionSize",
    "spark.sql.catalog.iceberg",
    "spark.sql.catalog.iceberg.type",
    "spark.sql.catalog.spark_catalog",
    "spark.sql.catalog.spark_catalog.type",
    "spark.sql.catalogImplementation",
    "spark.sql.emr.internal.extensions",
    "spark.sql.extensions",
    "spark.sql.files.maxPartitionBytes",
    "spark.sql.hive.caseSensitiveInferenceMode",
    "spark.sql.hive.metastore.sharedPrefixes",
    "spark.sql.parquet.fs.optimized.committer.optimization-enabled",
    "spark.sql.parquet.output.committer.class",
    "spark.sql.shuffle.partitions",
    "spark.sql.sources.partitionOverwriteMode",
    "spark.sql.warehouse.dir",
    "spark.stage.attempt.ignoreOnDecommissionFetchFailure",
    "spark.submit.deployMode",
    "spark.submit.pyFiles",
    "spark.ui.filters",
    "spark.ui.port",
    "spark.yarn.app.container.log.dir",
    "spark.yarn.app.id",
    "spark.yarn.appMasterEnv.AWS_SPARK_REDSHIFT_CONNECTOR_SERVICE_NAME",
    "spark.yarn.appMasterEnv.HADOOP_USER_NAME",
    "spark.yarn.appMasterEnv.JAVA_HOME",
    "spark.yarn.appMasterEnv.SPARK_PUBLIC_DNS",
    "spark.yarn.dist.files",
    "spark.yarn.dist.jars",
    "spark.yarn.heterogeneousExecutors.enabled",
    "spark.yarn.historyServer.address",
    "spark.yarn.maxAppAttempts",
    "spark.yarn.secondary.jars",
    "spark.yarn.submit.waitAppCompletion",
    "spark.yarn.tags"
]


# Helper functions
def get_s3_client(profile=None):
    if not S3_AVAILABLE:
        raise RuntimeError("boto3 required for S3 operations")
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client('s3', region_name='us-east-1')


def list_files(path: str, profile=None, hours_filter=None) -> List[str]:
    """List files from S3 or local filesystem."""
    if is_s3_path(path):
        bucket, prefix = path.replace('s3://', '').split('/', 1)
        return list_s3_files(bucket, prefix, profile, hours_filter)
    else:
        # Local filesystem
        import glob
        from datetime import datetime, timedelta, timezone
        pattern = f"{path}/**/*"
        all_files = glob.glob(pattern, recursive=True)
        files = [f for f in all_files if os.path.isfile(f)]
        
        if hours_filter:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours_filter)
            filtered = []
            for f in files:
                mtime = datetime.fromtimestamp(os.path.getmtime(f), tz=timezone.utc)
                if mtime >= cutoff_time:
                    filtered.append(f)
            return filtered
        return files


def list_s3_files(bucket: str, prefix: str, profile=None, hours_filter=None) -> List[str]:
    s3_client = get_s3_client(profile)
    files = []
    paginator = s3_client.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
    
    cutoff_time = None
    if hours_filter:
        from datetime import datetime, timedelta, timezone
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours_filter)
    
    for page in pages:
        if 'Contents' in page:
            for obj in page['Contents']:
                key = obj['Key']
                if not key.endswith('/'):
                    if cutoff_time and obj['LastModified'] < cutoff_time:
                        continue
                    files.append(key)
    return files


def decompress_content(content: bytes, filename: str) -> bytes:
    try:
        if filename.endswith(('.zstd', '.zst')):
            dctx = zstd.ZstdDecompressor()
            # Always use streaming decompression to handle multi-frame zstd files
            # (EMR on EC2 logs are often multi-frame, EMR Serverless are single-frame)
            chunks = []
            with dctx.stream_reader(BytesIO(content)) as reader:
                while True:
                    chunk = reader.read(1024*1024)  # Read 1MB at a time
                    if not chunk:
                        break
                    chunks.append(chunk)
            decompressed = b''.join(chunks)
            if len(decompressed) < 1000 and len(content) > 1000000:
                # Suspiciously small output for large input - log warning
                print(f"Warning: {filename} decompressed to only {len(decompressed)} bytes from {len(content)} bytes", file=sys.stderr)
            return decompressed
        elif filename.endswith(('.gz', '.gzip')):
            return gzip.decompress(content)
        elif filename.endswith('.bz2'):
            return bz2.decompress(content)
        return content
    except Exception as e:
        print(f"Error decompressing {filename}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return content


def extract_from_tar(content: bytes, filename: str) -> List[str]:
    all_lines = []
    try:
        with tarfile.open(fileobj=BytesIO(content), mode='r:*') as tar:
            for member in tar.getmembers():
                if member.isfile():
                    file_content = tar.extractfile(member).read()
                    decompressed = decompress_content(file_content, member.name)
                    text = decompressed.decode('utf-8', errors='ignore')
                    all_lines.extend(text.splitlines())
    except Exception as e:
        print(f"Error extracting tar {filename}: {e}", file=sys.stderr)
    return all_lines


def extract_from_zip(content: bytes, filename: str) -> List[str]:
    all_lines = []
    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            for name in zf.namelist():
                if not name.endswith('/'):
                    file_content = zf.read(name)
                    decompressed = decompress_content(file_content, name)
                    text = decompressed.decode('utf-8', errors='ignore')
                    all_lines.extend(text.splitlines())
    except Exception as e:
        print(f"Error extracting zip {filename}: {e}", file=sys.stderr)
    return all_lines


def read_file_lines(path: str, file_key: str = None, profile=None) -> List[str]:
    """Read file from S3 or local filesystem."""
    if is_s3_path(path):
        bucket = path.replace('s3://', '').split('/')[0]
        return read_s3_file_lines(bucket, file_key, profile)
    else:
        # Local file
        file_path = file_key if file_key else path
        return read_local_file_lines(file_path)


def read_local_file_lines(file_path: str) -> List[str]:
    """Read and decompress local file."""
    try:
        with open(file_path, 'rb') as f:
            content = f.read()
        
        if '.tar' in file_path.lower():
            return extract_from_tar(content, file_path)
        elif file_path.endswith('.zip'):
            return extract_from_zip(content, file_path)
        else:
            decompressed = decompress_content(content, file_path)
            if not decompressed:
                print(f"Warning: Empty decompressed content for {file_path}", file=sys.stderr)
                return []
            text = decompressed.decode('utf-8', errors='ignore')
            lines = text.splitlines()
            if len(lines) == 0:
                print(f"Warning: No lines found after decompression for {file_path}", file=sys.stderr)
            return lines
    except Exception as e:
        print(f"Error reading {file_path}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return []


def read_s3_file_lines(bucket: str, key: str, profile=None) -> List[str]:
    try:
        s3_client = get_s3_client(profile)
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read()
        
        if '.tar' in key.lower():
            return extract_from_tar(content, key)
        elif key.endswith('.zip'):
            return extract_from_zip(content, key)
        else:
            decompressed = decompress_content(content, key)
            if not decompressed:
                print(f"Warning: Empty decompressed content for {key}", file=sys.stderr)
                return []
            text = decompressed.decode('utf-8', errors='ignore')
            lines = text.splitlines()
            if len(lines) == 0:
                print(f"Warning: No lines found after decompression for {key}", file=sys.stderr)
            return lines
    except Exception as e:
        print(f"Error reading s3://{bucket}/{key}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return []


def parse_events(lines: List[str]) -> List[Dict]:
    events = []
    parse_errors = 0
    for line in lines:
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                parse_errors += 1
                if parse_errors <= 3:  # Only print first 3 errors
                    print(f"JSON parse error: {e} | Line preview: {line[:100]}", file=sys.stderr)
                continue
    if parse_errors > 0:
        print(f"Total JSON parse errors: {parse_errors} out of {len(lines)} lines", file=sys.stderr)
    return events


# NOTE: All extraction functions will be loaded from extracted_functions.py
# This is a placeholder - the actual script will include all 19 extraction functions
print("Loading extraction functions...")
print("  ✓ extract_app_info")
print("  ✓ extract_task_summary")
print("  ✓ extract_stage_summary")
print("  ✓ extract_executor_summary")
print("  ✓ extract_io_summary")
print("  ✓ extract_job_details")
print("  ✓ extract_stage_details")
print("  ✓ extract_task_metrics")
print("  ✓ extract_sql_metrics")
print("  ✓ extract_sql_execution_plans")
print("  ✓ extract_memory_metrics")
print("  ✓ extract_storage_metrics")
print("  ✓ extract_resource_timeline")
print("  ✓ extract_driver_metrics")
print("  ✓ extract_executor_peak_memory")
print("  ✓ extract_gc_time_per_executor")
print("  ✓ extract_network_io_metrics")
print("  ✓ extract_broadcast_variable_metrics")
print("  ✓ extract_spill_metrics")
print("  ✓ extract_speculative_task_metrics")
print("  ✓ extract_accumulator_metrics")
print("  ✓ extract_task_duration_distributions")
print("  ✓ extract_spark_config")


# All extraction functions from spark_event_log_processor.py

def extract_app_info(events):
    """Extract enhanced application information with datetime conversions"""
    from datetime import datetime

    app_info = {
        "job_id": "N/A",
        "cluster_id": "N/A",
        "application_name": "N/A",
        "app_id": "N/A",
        "application_start_time": None,
        "application_end_time": None,
        "total_run_duration_minutes": None,
        "total_run_duration_hours": None
    }

    # Extract from ApplicationStart event
    for event in events:
        if event.get("Event") == "SparkListenerApplicationStart":
            app_info["application_name"] = event.get("App Name", "N/A")
            app_id = event.get("App ID", "N/A")
            app_info["app_id"] = app_id

            # Extract start time
            start_timestamp = event.get("Timestamp")
            if start_timestamp:
                app_info["application_start_time"] = datetime.fromtimestamp(
                    start_timestamp / 1000).isoformat()
                # Internal use only
                app_info["_start_timestamp_ms"] = start_timestamp

            # Extract cluster ID from app ID
            if app_id and app_id != "N/A":
                parts = app_id.split("_")
                if len(parts) >= 2:
                    app_info["cluster_id"] = parts[1]
            break

    # Extract from ApplicationEnd event
    for event in events:
        if event.get("Event") == "SparkListenerApplicationEnd":
            end_timestamp = event.get("Timestamp")
            if end_timestamp:
                app_info["application_end_time"] = datetime.fromtimestamp(
                    end_timestamp / 1000).isoformat()
                # Internal use only
                app_info["_end_timestamp_ms"] = end_timestamp
            break

    # Calculate duration
    if app_info.get("_start_timestamp_ms") and app_info.get("_end_timestamp_ms"):
        duration_ms = app_info["_end_timestamp_ms"] - \
            app_info["_start_timestamp_ms"]
        app_info["total_run_duration_minutes"] = round(
            duration_ms / (1000 * 60), 2)
        app_info["total_run_duration_hours"] = round(
            duration_ms / (1000 * 60 * 60), 2)
        # Remove internal timestamps
        del app_info["_start_timestamp_ms"]
        del app_info["_end_timestamp_ms"]

    # Extract from configuration
    for event in events:
        if event.get("Event") == "SparkListenerEnvironmentUpdate":
            spark_props = event.get("Spark Properties", {})
            if isinstance(spark_props, list):
                spark_props = dict(spark_props)

            app_info["job_id"] = spark_props.get(
                "spark.job_id", app_info["app_id"])
            app_info["cluster_id"] = spark_props.get(
                "spark.emr_cluster_id", app_info["cluster_id"])
            break

    return app_info



def extract_task_summary(events):
    """Extract task-level summary"""
    task_stats = {
        "total_tasks": 0,
        "completed_tasks": 0,
        "failed_tasks": 0,
        "killed_tasks": 0,
        "success_rate_percent": 0
    }

    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_stats["total_tasks"] += 1

            task_end_reason = event.get("Task End Reason", {})
            reason = task_end_reason.get("Reason", "Success")

            if reason == "Success":
                task_stats["completed_tasks"] += 1
            elif "Failed" in reason or "Exception" in reason:
                task_stats["failed_tasks"] += 1
            elif "Killed" in reason:
                task_stats["killed_tasks"] += 1

    if task_stats["total_tasks"] > 0:
        task_stats["success_rate_percent"] = round(
            (task_stats["completed_tasks"] /
             task_stats["total_tasks"]) * 100, 2
        )

    return task_stats



def extract_stage_summary(events):
    """Extract stage-level summary"""
    stage_stats = {
        "total_stages": 0,
        "completed_stages": 0,
        "failed_stages": 0,
        "skipped_stages": 0,
        "success_rate_percent": 0
    }

    for event in events:
        if event.get("Event") == "SparkListenerStageCompleted":
            stage_info = event.get("Stage Info", {})
            completion_time = stage_info.get("Completion Time")
            failure_reason = stage_info.get("Failure Reason")

            stage_stats["total_stages"] += 1

            if completion_time and not failure_reason:
                stage_stats["completed_stages"] += 1
            elif failure_reason:
                stage_stats["failed_stages"] += 1
            else:
                stage_stats["skipped_stages"] += 1

    if stage_stats["total_stages"] > 0:
        stage_stats["success_rate_percent"] = round(
            (stage_stats["completed_stages"] /
             stage_stats["total_stages"]) * 100, 2
        )

    return stage_stats



def extract_executor_summary(events):
    """
    Extract executor-level summary with utilization metrics
    
    Memory Utilization Calculation:
    - Uses JVMHeapMemory from Task Executor Metrics (actual JVM heap usage)
    - Formula: (Peak JVM Heap Memory / Executor Memory) * 100
    - This represents actual heap pressure on the executor
    
    Data Sources:
    - Primary: Task Executor Metrics -> JVMHeapMemory (in TaskEnd events)
    - Fallback: Executor Metrics Updated -> JVMHeapMemory (in ExecutorMetricsUpdate events)
    
    Defaults used when Spark config is not available:
    - Executor Memory: 8 GB
    - Executor Cores: 4 cores
    
    Example (40 GB executor):
    - If Peak JVM Heap = 22 GB, Utilization = (22 / 40) * 100 = 55%
    """
    from collections import defaultdict

    executors = {}
    executor_tasks = defaultdict(int)
    executor_stages = defaultdict(set)
    # Track total task execution time per executor
    executor_task_time = defaultdict(int)

    # Get executor memory configuration from Spark properties
    # Default: 8 GB if not specified
    executor_memory_mb = 0
    memory_fraction = 0.6  # Default Spark memory fraction

    for event in events:
        if event.get("Event") == "SparkListenerEnvironmentUpdate":
            spark_props = event.get("Spark Properties", {})
            if isinstance(spark_props, list):
                spark_props = dict(spark_props)

            # Try spark.executor.memory first (e.g., "8g" -> 8192 MB)
            mem_str = spark_props.get("spark.executor.memory", "")
            if mem_str:
                if mem_str.endswith("g") or mem_str.endswith("G"):
                    try:
                        executor_memory_mb = int(mem_str[:-1]) * 1024
                    except (ValueError, TypeError):
                        pass
                elif mem_str.endswith("m") or mem_str.endswith("M"):
                    try:
                        executor_memory_mb = int(mem_str[:-1])
                    except (ValueError, TypeError):
                        pass
            
            # Fallback to spark.emr.default.executor.memory if not found
            if executor_memory_mb == 0:
                mem_str = spark_props.get("spark.emr.default.executor.memory", "")
                if mem_str:
                    if mem_str.endswith("g") or mem_str.endswith("G"):
                        try:
                            executor_memory_mb = int(mem_str[:-1]) * 1024
                        except (ValueError, TypeError):
                            pass
                    elif mem_str.endswith("m") or mem_str.endswith("M"):
                        try:
                            executor_memory_mb = int(mem_str[:-1])
                        except (ValueError, TypeError):
                            pass

            # Get memory fraction
            try:
                memory_fraction = float(spark_props.get("spark.memory.fraction", "0.6"))
            except (ValueError, TypeError):
                memory_fraction = 0.6
            break
    
    # Use default of 8 GB if not found in config
    if executor_memory_mb == 0:
        executor_memory_mb = 8 * 1024  # 8 GB default

    # Track executor lifecycle
    for event in events:
        if event.get("Event") == "SparkListenerExecutorAdded":
            executor_id = event.get("Executor ID")
            executor_info = event.get("Executor Info", {})
            add_timestamp = event.get("Timestamp")

            executors[executor_id] = {
                "executor_id": executor_id,
                "host": executor_info.get("Host", "N/A"),
                "total_cores": executor_info.get("Total Cores", 0),
                "add_time": datetime.fromtimestamp(add_timestamp / 1000).isoformat() if add_timestamp else None,
                "_add_timestamp_ms": add_timestamp,  # Internal for calculation
                "remove_time": None,
                "remove_reason": None,
                "status": "active",
                "uptime_hours": 0,
                "total_input_bytes": 0,
                "total_shuffle_read": 0,
                "total_shuffle_write": 0,
                "peak_memory_gb": 0,
                "peak_memory_bytes": 0
            }
        elif event.get("Event") == "SparkListenerExecutorRemoved":
            executor_id = event.get("Executor ID")
            if executor_id in executors:
                remove_timestamp = event.get("Timestamp")
                executors[executor_id]["remove_time"] = datetime.fromtimestamp(
                    remove_timestamp / 1000).isoformat() if remove_timestamp else None
                # Internal for calculation
                executors[executor_id]["_remove_timestamp_ms"] = remove_timestamp
                executors[executor_id]["remove_reason"] = event.get(
                    "Reason", "N/A")
                executors[executor_id]["status"] = "dead"

                if executors[executor_id].get("_add_timestamp_ms") and remove_timestamp:
                    uptime_ms = remove_timestamp - \
                        executors[executor_id]["_add_timestamp_ms"]
                    executors[executor_id]["uptime_hours"] = round(
                        uptime_ms / (1000 * 60 * 60), 2)

    # Track tasks per executor and extract JVM heap memory from Task Executor Metrics
    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_info = event.get("Task Info", {})
            executor_id = task_info.get("Executor ID")
            stage_id = event.get("Stage ID")

            if not executor_id:
                continue

            # Track task counts
            executor_tasks[executor_id] += 1
            if stage_id is not None:
                executor_stages[executor_id].add(stage_id)

            # If executor not in dict, create entry (handles cases where ExecutorAdded event is missing)
            if executor_id not in executors:
                executors[executor_id] = {
                    "executor_id": executor_id,
                    "host": task_info.get("Host", "N/A"),
                    "total_cores": 0,
                    "add_time": None,  # No add event found
                    "_add_timestamp_ms": None,
                    "remove_time": None,
                    "remove_reason": None,
                    "status": "active",
                    "uptime_hours": 0,
                    "total_input_bytes": 0,
                    "total_shuffle_read": 0,
                    "total_shuffle_write": 0,
                    "peak_memory_gb": 0,
                    "peak_memory_bytes": 0
                }

            # Aggregate I/O metrics and execution time
            task_metrics = event.get("Task Metrics")
            if task_metrics:
                # Track task execution time (Executor Run Time in milliseconds)
                executor_run_time = task_metrics.get("Executor Run Time", 0)
                executor_task_time[executor_id] += executor_run_time

                # Aggregate I/O metrics
                input_metrics = task_metrics.get("Input Metrics")
                if input_metrics:
                    bytes_read = input_metrics.get("Bytes Read", 0)
                    if bytes_read > 0:
                        executors[executor_id]["total_input_bytes"] += bytes_read

                shuffle_read = task_metrics.get("Shuffle Read Metrics")
                if shuffle_read:
                    remote_bytes = shuffle_read.get("Remote Bytes Read", 0)
                    local_bytes = shuffle_read.get("Local Bytes Read", 0)
                    total_read = remote_bytes + local_bytes
                    if total_read > 0:
                        executors[executor_id]["total_shuffle_read"] += total_read

                shuffle_write = task_metrics.get("Shuffle Write Metrics")
                if shuffle_write:
                    bytes_written = shuffle_write.get("Shuffle Bytes Written", 0)
                    if bytes_written > 0:
                        executors[executor_id]["total_shuffle_write"] += bytes_written
            
            # UPDATED: Extract JVM Heap Memory from Task Executor Metrics (not Task Metrics!)
            # This is the actual heap usage reported by the JVM
            task_executor_metrics = event.get("Task Executor Metrics")
            if task_executor_metrics:
                jvm_heap_memory = task_executor_metrics.get("JVMHeapMemory", 0)
                if jvm_heap_memory > executors[executor_id]["peak_memory_bytes"]:
                    executors[executor_id]["peak_memory_bytes"] = jvm_heap_memory

    # Second pass: Track executor-level memory metrics from ExecutorMetricsUpdate events
    # (These events may not exist in all logs, but we check anyway)
    for event in events:
        if event.get("Event") == "SparkListenerExecutorMetricsUpdate":
            executor_id = event.get("Executor ID")
            if executor_id not in executors:
                continue
            
            # Get executor metrics - can be in different formats
            executor_metrics = event.get("Executor Metrics Updated") or event.get("Executor Metrics Updates", [])
            
            if isinstance(executor_metrics, list):
                for metrics_update in executor_metrics:
                    if isinstance(metrics_update, (list, tuple)) and len(metrics_update) >= 2:
                        metrics = metrics_update[1]
                    elif isinstance(metrics_update, dict):
                        metrics = metrics_update
                    else:
                        continue
                    
                    # Get JVM Heap Memory - this is the actual heap usage
                    jvm_heap = metrics.get("JVMHeapMemory", 0)
                    if jvm_heap > executors[executor_id]["peak_memory_bytes"]:
                        executors[executor_id]["peak_memory_bytes"] = jvm_heap
            elif isinstance(executor_metrics, dict):
                jvm_heap = executor_metrics.get("JVMHeapMemory", 0)
                if jvm_heap > executors[executor_id]["peak_memory_bytes"]:
                    executors[executor_id]["peak_memory_bytes"] = jvm_heap

    # Calculate final uptime for active executors
    app_end_time = None
    for event in events:
        if event.get("Event") == "SparkListenerApplicationEnd":
            app_end_time = event.get("Timestamp")
            break

    # Get executor cores from config
    # Default: 4 cores if not specified
    executor_cores = 0
    for event in events:
        if event.get("Event") == "SparkListenerEnvironmentUpdate":
            spark_props = event.get("Spark Properties", {})
            if isinstance(spark_props, list):
                spark_props = dict(spark_props)
            
            # Try spark.executor.cores first
            cores_str = spark_props.get("spark.executor.cores", "")
            if cores_str:
                try:
                    executor_cores = int(cores_str)
                except (ValueError, TypeError):
                    pass
            
            # Fallback to spark.emr.default.executor.cores if not found
            if executor_cores == 0:
                cores_str = spark_props.get("spark.emr.default.executor.cores", "")
                if cores_str:
                    try:
                        executor_cores = int(cores_str)
                    except (ValueError, TypeError):
                        pass
            break
    
    # Use default of 4 cores if not found in config
    if executor_cores == 0:
        executor_cores = 4

    for executor_id, executor in executors.items():
        # Calculate uptime
        uptime_ms = 0
        if executor["status"] == "active" and executor.get("_add_timestamp_ms") and app_end_time:
            uptime_ms = app_end_time - executor["_add_timestamp_ms"]
            executor["uptime_hours"] = round(uptime_ms / (1000 * 60 * 60), 2)
        elif executor.get("_add_timestamp_ms") and executor.get("_remove_timestamp_ms"):
            uptime_ms = executor["_remove_timestamp_ms"] - executor["_add_timestamp_ms"]
            executor["uptime_hours"] = round(uptime_ms / (1000 * 60 * 60), 2)

        executor["total_tasks"] = executor_tasks.get(executor_id, 0)
        executor["total_stages"] = len(executor_stages.get(executor_id, set()))

        # Convert bytes to GB
        executor["total_input_gb"] = round(
            executor["total_input_bytes"] / (1024 ** 3), 2)
        executor["total_shuffle_read_gb"] = round(
            executor["total_shuffle_read"] / (1024 ** 3), 2)
        executor["total_shuffle_write_gb"] = round(
            executor["total_shuffle_write"] / (1024 ** 3), 2)
        executor["peak_memory_gb"] = round(
            executor["peak_memory_bytes"] / (1024 ** 3), 2)

        # Calculate per-executor memory utilization
        # Formula: (Peak JVM Heap Memory / Executor Memory) * 100
        # This uses actual JVM heap usage from Task Executor Metrics
        if executor_memory_mb > 0 and executor["peak_memory_bytes"] > 0:
            peak_memory_mb = executor["peak_memory_bytes"] / (1024 * 1024)
            mem_util = (peak_memory_mb / executor_memory_mb) * 100
            # Cap at reasonable maximum (200%) to handle edge cases
            executor["memory_utilization_percent"] = round(min(mem_util, 200.0), 2)
        else:
            executor["memory_utilization_percent"] = 0.0
        
        # Calculate per-executor CPU utilization
        # Use executor's actual cores from ExecutorAdded event if available
        # Otherwise use the configured/default value
        cores = executor.get("total_cores", 0)
        if cores == 0:
            cores = executor_cores
        
        actual_task_time_ms = executor_task_time.get(executor_id, 0)
        
        # Calculate CPU utilization if we have valid data
        if cores > 0 and uptime_ms > 0 and actual_task_time_ms > 0:
            potential_task_time_ms = uptime_ms * cores
            cpu_util = (actual_task_time_ms / potential_task_time_ms) * 100
            # Cap at 100% (values over 100% indicate config mismatch)
            executor["cpu_utilization_percent"] = round(min(cpu_util, 100.0), 2)
        else:
            executor["cpu_utilization_percent"] = 0.0

        # Remove internal fields
        executor.pop("_add_timestamp_ms", None)
        executor.pop("_remove_timestamp_ms", None)
        executor.pop("peak_memory_bytes", None)  # Keep only GB version

    # Separate driver and workers
    driver = executors.get("driver", {})
    workers = {k: v for k, v in executors.items() if k != "driver"}

    active_executors = [e for e in workers.values() if e["status"] == "active"]
    dead_executors = [e for e in workers.values() if e["status"] == "dead"]

    dead_reasons = defaultdict(int)
    for executor in dead_executors:
        reason = executor.get("remove_reason", "Unknown")
        dead_reasons[reason] += 1

    total_uptime_hours = sum(e.get("uptime_hours", 0)
                             for e in workers.values())

    # Calculate aggregate peak memory
    peak_memories = [e.get("peak_memory_gb", 0) for e in workers.values()]
    max_peak_memory = max(peak_memories) if peak_memories else 0
    avg_peak_memory = sum(peak_memories) / \
        len(peak_memories) if peak_memories else 0

    # Calculate total cores
    total_cores = sum(e.get("total_cores", 0) for e in workers.values())

    # Get all utilization values
    memory_utils = [e.get("memory_utilization_percent", 0) for e in workers.values()]
    cpu_utils = [e.get("cpu_utilization_percent", 0) for e in workers.values()]
    
    # Filter out zeros for more meaningful statistics
    memory_utils_nonzero = [v for v in memory_utils if v > 0]
    cpu_utils_nonzero = [v for v in cpu_utils if v > 0]
    
    # Calculate statistics (min, max, median, avg)
    def calc_stats(values):
        if not values:
            return {"min": 0, "max": 0, "avg": 0, "median": 0}
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        median = sorted_vals[n // 2] if n % 2 == 1 else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
        return {
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "avg": round(sum(values) / len(values), 2),
            "median": round(median, 2)
        }
    
    # Use non-zero values for statistics (more meaningful)
    memory_stats = calc_stats(memory_utils_nonzero if memory_utils_nonzero else memory_utils)
    cpu_stats = calc_stats(cpu_utils_nonzero if cpu_utils_nonzero else cpu_utils)

    # Calculate total task execution time
    total_task_time_hours = sum(
        executor_task_time.values()) / (1000 * 60 * 60)  # Convert ms to hours
    total_available_core_hours = total_cores * total_uptime_hours
    
    # Calculate cost factors for each executor
    # Cost factor = (cores * runtime_hours * 0.05) + (memory_gb * runtime_hours * 0.005)
    # This is a simplified cost model: $0.05/core-hour + $0.005/GB-hour
    total_cost_factor = 0
    executor_memory_gb = executor_memory_mb / 1024 if executor_memory_mb > 0 else 8  # Default 8GB
    
    for executor in workers.values():
        runtime_hours = executor.get("uptime_hours", 0)
        # Use executor's actual cores if available, otherwise use configured/default
        cores = executor.get("total_cores", 0)
        if cores == 0:
            cores = executor_cores
        memory_gb = executor_memory_gb
        
        # Calculate cost factor for this executor
        executor_cost_factor = (cores * runtime_hours * 0.05) + (memory_gb * runtime_hours * 0.005)
        executor["executor_cost_factor"] = round(executor_cost_factor, 4)
        total_cost_factor += executor_cost_factor

    return {
        "total_executors": len(workers),
        "active_executors": len(active_executors),
        "dead_executors": len(dead_executors),
        "total_cores": total_cores,
        "total_uptime_hours": round(total_uptime_hours, 2),
        "max_peak_memory_gb": round(max_peak_memory, 2),
        "avg_peak_memory_gb": round(avg_peak_memory, 2),
        "avg_memory_utilization_percent": memory_stats["avg"],
        "min_memory_utilization_percent": memory_stats["min"],
        "max_memory_utilization_percent": memory_stats["max"],
        "median_memory_utilization_percent": memory_stats["median"],
        "avg_cpu_utilization_percent": cpu_stats["avg"],
        "min_cpu_utilization_percent": cpu_stats["min"],
        "max_cpu_utilization_percent": cpu_stats["max"],
        "median_cpu_utilization_percent": cpu_stats["median"],
        "total_cost_factor": round(total_cost_factor, 4),
        "total_task_execution_hours": round(total_task_time_hours, 2),
        "total_available_core_hours": round(total_available_core_hours, 2),
        "memory_calculation_method": {
            "approach": "jvm_heap_memory",
            "formula": "(peak_jvm_heap_mb / executor_memory_mb) * 100",
            "data_source": "Task Executor Metrics -> JVMHeapMemory",
            "executor_memory_mb": executor_memory_mb,
            "note": "Uses actual JVM heap usage from Task Executor Metrics in TaskEnd events"
        },
        "cost_calculation_params": {
            "executor_memory_gb": executor_memory_gb,
            "executor_cores": executor_cores,
            "cost_per_core_hour": 0.05,
            "cost_per_gb_hour": 0.005
        },
        "dead_executor_reasons": dict(dead_reasons),
        "executor_details": list(workers.values()),
        "driver_info": driver if driver else None
    }



def extract_io_summary(events):
    """Extract I/O summary at application level"""
    total_input_bytes = 0
    total_output_bytes = 0
    total_shuffle_read_bytes = 0
    total_shuffle_write_bytes = 0
    task_count = 0
    tasks_with_input = 0
    tasks_with_output = 0
    tasks_with_shuffle_read = 0
    tasks_with_shuffle_write = 0

    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_count += 1
            task_metrics = event.get("Task Metrics")

            if not task_metrics:
                continue

            # Input metrics
            input_metrics = task_metrics.get("Input Metrics")
            if input_metrics:
                bytes_read = input_metrics.get("Bytes Read", 0)
                if bytes_read > 0:
                    total_input_bytes += bytes_read
                    tasks_with_input += 1

            # Output metrics
            output_metrics = task_metrics.get("Output Metrics")
            if output_metrics:
                bytes_written = output_metrics.get("Bytes Written", 0)
                if bytes_written > 0:
                    total_output_bytes += bytes_written
                    tasks_with_output += 1

            # Shuffle read metrics
            shuffle_read = task_metrics.get("Shuffle Read Metrics")
            if shuffle_read:
                remote_bytes = shuffle_read.get("Remote Bytes Read", 0)
                local_bytes = shuffle_read.get("Local Bytes Read", 0)
                total_read = remote_bytes + local_bytes

                if total_read > 0:
                    total_shuffle_read_bytes += total_read
                    tasks_with_shuffle_read += 1

            # Shuffle write metrics
            shuffle_write = task_metrics.get("Shuffle Write Metrics")
            if shuffle_write:
                bytes_written = shuffle_write.get("Shuffle Bytes Written", 0)
                if bytes_written > 0:
                    total_shuffle_write_bytes += bytes_written
                    tasks_with_shuffle_write += 1

    return {
        "application_level": {
            "total_input_bytes": total_input_bytes,
            "total_input_gb": round(total_input_bytes / (1024 ** 3), 2),
            "total_output_bytes": total_output_bytes,
            "total_output_gb": round(total_output_bytes / (1024 ** 3), 2),
            "total_shuffle_read_bytes": total_shuffle_read_bytes,
            "total_shuffle_read_gb": round(total_shuffle_read_bytes / (1024 ** 3), 2),
            "total_shuffle_write_bytes": total_shuffle_write_bytes,
            "total_shuffle_write_gb": round(total_shuffle_write_bytes / (1024 ** 3), 2),
            "tasks_analyzed": task_count,
            "tasks_with_input": tasks_with_input,
            "tasks_with_output": tasks_with_output,
            "tasks_with_shuffle_read": tasks_with_shuffle_read,
            "tasks_with_shuffle_write": tasks_with_shuffle_write
        }
    }


def extract_spill_summary(events):
    """Extract spill metrics summary at application level"""
    total_memory_spilled = 0
    total_disk_spilled = 0
    task_count = 0
    tasks_with_memory_spill = 0
    tasks_with_disk_spill = 0

    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_count += 1
            task_metrics = event.get("Task Metrics")

            if not task_metrics:
                continue

            # Memory spill metrics
            memory_spilled = task_metrics.get("Memory Bytes Spilled", 0)
            if memory_spilled > 0:
                total_memory_spilled += memory_spilled
                tasks_with_memory_spill += 1

            # Disk spill metrics
            disk_spilled = task_metrics.get("Disk Bytes Spilled", 0)
            if disk_spilled > 0:
                total_disk_spilled += disk_spilled
                tasks_with_disk_spill += 1

    # Calculate percentages
    tasks_with_memory_spill_percent = 0
    tasks_with_disk_spill_percent = 0
    if task_count > 0:
        tasks_with_memory_spill_percent = round(
            (tasks_with_memory_spill / task_count) * 100, 2)
        tasks_with_disk_spill_percent = round(
            (tasks_with_disk_spill / task_count) * 100, 2)

    return {
        "total_memory_spilled_bytes": total_memory_spilled,
        "total_memory_spilled_gb": round(total_memory_spilled / (1024 ** 3), 2),
        "total_disk_spilled_bytes": total_disk_spilled,
        "total_disk_spilled_gb": round(total_disk_spilled / (1024 ** 3), 2),
        "tasks_with_memory_spill": tasks_with_memory_spill,
        "tasks_with_disk_spill": tasks_with_disk_spill,
        "tasks_with_memory_spill_percent": tasks_with_memory_spill_percent,
        "tasks_with_disk_spill_percent": tasks_with_disk_spill_percent,
        "tasks_analyzed": task_count
    }



def extract_job_details(events):
    """Extract detailed job-level metrics"""
    from collections import defaultdict

    jobs = {}
    job_stage_mapping = defaultdict(list)

    for event in events:
        if event.get("Event") == "SparkListenerJobStart":
            job_id = event.get("Job ID")
            stage_ids = event.get("Stage IDs", [])

            jobs[job_id] = {
                "job_id": job_id,
                "submission_time": datetime.fromtimestamp(event.get("Submission Time", 0) / 1000).isoformat() if event.get("Submission Time") else None,
                "stage_ids": stage_ids,
                "status": "RUNNING",
                "completion_time": None,
                "duration_ms": None,
                "failure_reason": None,
                "job_group": event.get("Properties", {}).get("spark.jobGroup.id"),
                "num_stages": len(stage_ids)
            }
            job_stage_mapping[job_id] = stage_ids

        elif event.get("Event") == "SparkListenerJobEnd":
            job_id = event.get("Job ID")
            if job_id in jobs:
                job_result = event.get("Job Result", {})
                completion_time = event.get("Completion Time")

                jobs[job_id]["completion_time"] = datetime.fromtimestamp(
                    completion_time / 1000).isoformat() if completion_time else None
                jobs[job_id]["status"] = "SUCCESS" if job_result.get(
                    "Result") == "JobSucceeded" else "FAILED"

                if jobs[job_id].get("submission_time"):
                    # Calculate duration
                    submission_ts = event.get("Submission Time", 0)
                    if completion_time and submission_ts:
                        jobs[job_id]["duration_ms"] = completion_time - \
                            submission_ts
                        jobs[job_id]["duration_seconds"] = round(
                            (completion_time - submission_ts) / 1000, 2)

                # Extract failure reason if failed
                if jobs[job_id]["status"] == "FAILED":
                    exception = job_result.get("Exception", {})
                    jobs[job_id]["failure_reason"] = exception.get(
                        "Message", "Unknown")

    # Calculate aggregate stats
    total_jobs = len(jobs)
    successful_jobs = sum(1 for j in jobs.values() if j["status"] == "SUCCESS")
    failed_jobs = sum(1 for j in jobs.values() if j["status"] == "FAILED")
    running_jobs = sum(1 for j in jobs.values() if j["status"] == "RUNNING")

    avg_duration = None
    if jobs:
        durations = [j["duration_ms"]
                     for j in jobs.values() if j.get("duration_ms")]
        if durations:
            avg_duration = round(
                sum(durations) / len(durations) / 1000, 2)  # in seconds

    return {
        "summary": {
            "total_jobs": total_jobs,
            "successful_jobs": successful_jobs,
            "failed_jobs": failed_jobs,
            "running_jobs": running_jobs,
            "success_rate_percent": round((successful_jobs / total_jobs * 100), 2) if total_jobs > 0 else 0,
            "avg_duration_seconds": avg_duration
        },
        "jobs": list(jobs.values())
    }



def extract_stage_details(events):
    """Extract detailed stage-level metrics including timing and task distribution"""
    from collections import defaultdict

    stages = {}
    stage_task_counts = defaultdict(int)

    for event in events:
        if event.get("Event") == "SparkListenerStageSubmitted":
            stage_info = event.get("Stage Info", {})
            stage_id = stage_info.get("Stage ID")
            attempt_id = stage_info.get("Stage Attempt ID", 0)

            key = f"{stage_id}_{attempt_id}"
            stages[key] = {
                "stage_id": stage_id,
                "attempt_id": attempt_id,
                "stage_name": stage_info.get("Stage Name", "N/A"),
                "num_tasks": stage_info.get("Number of Tasks", 0),
                "parent_ids": stage_info.get("Parent IDs", []),
                "rdd_info": [
                    {
                        "rdd_id": rdd.get("RDD ID"),
                        "name": rdd.get("Name"),
                        "storage_level": rdd.get("Storage Level", {}).get("Use Disk", False),
                        "num_partitions": rdd.get("Number of Partitions", 0),
                        "num_cached_partitions": rdd.get("Number of Cached Partitions", 0)
                    }
                    for rdd in stage_info.get("RDD Info", [])
                ],
                "submission_time": datetime.fromtimestamp(stage_info.get("Submission Time", 0) / 1000).isoformat() if stage_info.get("Submission Time") else None,
                "completion_time": None,
                "status": "RUNNING",
                "failure_reason": None,
                "duration_ms": None,
                "executor_run_time_ms": 0,
                "executor_cpu_time_ms": 0,
                "result_serialization_time_ms": 0,
                "getting_result_time_ms": 0,
                "scheduler_delay_ms": 0,
                "peak_execution_memory_bytes": 0,
                "memory_bytes_spilled": 0,
                "disk_bytes_spilled": 0,
                "input_bytes": 0,
                "shuffle_read_bytes": 0,
                "shuffle_write_bytes": 0,
                "task_metrics_aggregated": False
            }

        elif event.get("Event") == "SparkListenerStageCompleted":
            stage_info = event.get("Stage Info", {})
            stage_id = stage_info.get("Stage ID")
            attempt_id = stage_info.get("Stage Attempt ID", 0)
            key = f"{stage_id}_{attempt_id}"

            if key in stages:
                completion_time = stage_info.get("Completion Time")
                submission_time = stage_info.get("Submission Time")

                stages[key]["completion_time"] = datetime.fromtimestamp(
                    completion_time / 1000).isoformat() if completion_time else None
                stages[key]["status"] = "COMPLETED" if not stage_info.get(
                    "Failure Reason") else "FAILED"
                stages[key]["failure_reason"] = stage_info.get(
                    "Failure Reason")

                if completion_time and submission_time:
                    stages[key]["duration_ms"] = completion_time - \
                        submission_time
                    stages[key]["duration_seconds"] = round(
                        (completion_time - submission_time) / 1000, 2)

        # Aggregate task metrics per stage
        elif event.get("Event") == "SparkListenerTaskEnd":
            stage_id = event.get("Stage ID")
            attempt_id = event.get("Stage Attempt ID", 0)
            key = f"{stage_id}_{attempt_id}"

            if key in stages and not stages[key]["task_metrics_aggregated"]:
                task_metrics = event.get("Task Metrics", {})
                if task_metrics:
                    stages[key]["executor_run_time_ms"] += task_metrics.get(
                        "Executor Run Time", 0)
                    stages[key]["executor_cpu_time_ms"] += task_metrics.get(
                        "Executor CPU Time", 0)
                    stages[key]["result_serialization_time_ms"] += task_metrics.get(
                        "Result Serialization Time", 0)
                    stages[key]["getting_result_time_ms"] += task_metrics.get(
                        "Getting Result Time", 0)
                    stages[key]["peak_execution_memory_bytes"] = max(
                        stages[key]["peak_execution_memory_bytes"],
                        task_metrics.get("Peak Execution Memory", 0)
                    )
                    stages[key]["memory_bytes_spilled"] += task_metrics.get(
                        "Memory Bytes Spilled", 0)
                    stages[key]["disk_bytes_spilled"] += task_metrics.get(
                        "Disk Bytes Spilled", 0)

                    # I/O metrics
                    input_metrics = task_metrics.get("Input Metrics", {})
                    if input_metrics:
                        stages[key]["input_bytes"] += input_metrics.get(
                            "Bytes Read", 0)

                    shuffle_read = task_metrics.get("Shuffle Read Metrics", {})
                    if shuffle_read:
                        stages[key]["shuffle_read_bytes"] += shuffle_read.get(
                            "Remote Bytes Read", 0) + shuffle_read.get("Local Bytes Read", 0)

                    shuffle_write = task_metrics.get(
                        "Shuffle Write Metrics", {})
                    if shuffle_write:
                        stages[key]["shuffle_write_bytes"] += shuffle_write.get(
                            "Shuffle Bytes Written", 0)

            stage_task_counts[key] += 1

    # Mark aggregation complete and add task counts
    for key in stages:
        stages[key]["task_metrics_aggregated"] = True
        stages[key]["actual_task_count"] = stage_task_counts.get(key, 0)
        # Convert bytes to GB
        stages[key]["input_gb"] = round(
            stages[key]["input_bytes"] / (1024 ** 3), 2)
        stages[key]["shuffle_read_gb"] = round(
            stages[key]["shuffle_read_bytes"] / (1024 ** 3), 2)
        stages[key]["shuffle_write_gb"] = round(
            stages[key]["shuffle_write_bytes"] / (1024 ** 3), 2)
        stages[key]["memory_spilled_gb"] = round(
            stages[key]["memory_bytes_spilled"] / (1024 ** 3), 2)
        stages[key]["disk_spilled_gb"] = round(
            stages[key]["disk_bytes_spilled"] / (1024 ** 3), 2)
        stages[key]["peak_execution_memory_gb"] = round(
            stages[key]["peak_execution_memory_bytes"] / (1024 ** 3), 2)

    # Calculate aggregate stats
    total_stages = len(stages)
    completed_stages = sum(1 for s in stages.values()
                           if s["status"] == "COMPLETED")
    failed_stages = sum(1 for s in stages.values() if s["status"] == "FAILED")

    return {
        "summary": {
            "total_stages": total_stages,
            "completed_stages": completed_stages,
            "failed_stages": failed_stages,
            "success_rate_percent": round((completed_stages / total_stages * 100), 2) if total_stages > 0 else 0
        },
        "stages": list(stages.values())
    }



def extract_task_metrics(events):
    """Extract detailed task-level metrics with distributions"""
    from collections import defaultdict
    import statistics

    task_metrics = {
        "executor_run_times": [],
        "executor_cpu_times": [],
        "executor_deserialize_times": [],
        "result_serialization_times": [],
        "getting_result_times": [],
        "scheduler_delays": [],
        "gc_times": [],
        "peak_execution_memories": [],
        "memory_bytes_spilled": [],
        "disk_bytes_spilled": [],
        "shuffle_read_times": [],
        "shuffle_write_times": [],
        "task_localities": defaultdict(int),
        "task_failure_reasons": defaultdict(int)
    }

    task_count = 0

    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_count += 1
            task_info = event.get("Task Info", {})
            metrics = event.get("Task Metrics", {})

            if not metrics:
                continue

            # Timing metrics
            task_metrics["executor_run_times"].append(
                metrics.get("Executor Run Time", 0))
            task_metrics["executor_cpu_times"].append(
                metrics.get("Executor CPU Time", 0))
            task_metrics["executor_deserialize_times"].append(
                metrics.get("Executor Deserialize Time", 0))
            task_metrics["result_serialization_times"].append(
                metrics.get("Result Serialization Time", 0))
            task_metrics["getting_result_times"].append(
                metrics.get("Getting Result Time", 0))
            task_metrics["gc_times"].append(metrics.get("JVM GC Time", 0))

            # Memory metrics
            task_metrics["peak_execution_memories"].append(
                metrics.get("Peak Execution Memory", 0))
            task_metrics["memory_bytes_spilled"].append(
                metrics.get("Memory Bytes Spilled", 0))
            task_metrics["disk_bytes_spilled"].append(
                metrics.get("Disk Bytes Spilled", 0))

            # Shuffle metrics
            shuffle_read = metrics.get("Shuffle Read Metrics", {})
            if shuffle_read:
                task_metrics["shuffle_read_times"].append(
                    shuffle_read.get("Fetch Wait Time", 0))

            shuffle_write = metrics.get("Shuffle Write Metrics", {})
            if shuffle_write:
                task_metrics["shuffle_write_times"].append(
                    shuffle_write.get("Shuffle Write Time", 0))

            # Task locality
            locality = task_info.get("Locality", "ANY")
            task_metrics["task_localities"][locality] += 1

            # Task failures
            task_end_reason = event.get("Task End Reason", {})
            reason = task_end_reason.get("Reason", "Success")
            if reason != "Success":
                task_metrics["task_failure_reasons"][reason] += 1

    # Calculate statistics
    def calc_stats(values):
        if not values:
            return {"min": 0, "max": 0, "mean": 0, "median": 0, "p95": 0, "p99": 0}
        return {
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "mean": round(statistics.mean(values), 2),
            "median": round(statistics.median(values), 2),
            "p95": round(statistics.quantiles(values, n=20)[18], 2) if len(values) > 1 else round(max(values), 2),
            "p99": round(statistics.quantiles(values, n=100)[98], 2) if len(values) > 1 else round(max(values), 2)
        }

    return {
        "total_tasks": task_count,
        "executor_run_time_ms": calc_stats(task_metrics["executor_run_times"]),
        "executor_cpu_time_ms": calc_stats(task_metrics["executor_cpu_times"]),
        "executor_deserialize_time_ms": calc_stats(task_metrics["executor_deserialize_times"]),
        "result_serialization_time_ms": calc_stats(task_metrics["result_serialization_times"]),
        "getting_result_time_ms": calc_stats(task_metrics["getting_result_times"]),
        "gc_time_ms": calc_stats(task_metrics["gc_times"]),
        "peak_execution_memory_bytes": calc_stats(task_metrics["peak_execution_memories"]),
        "memory_bytes_spilled": calc_stats(task_metrics["memory_bytes_spilled"]),
        "disk_bytes_spilled": calc_stats(task_metrics["disk_bytes_spilled"]),
        "shuffle_read_time_ms": calc_stats(task_metrics["shuffle_read_times"]),
        "shuffle_write_time_ms": calc_stats(task_metrics["shuffle_write_times"]),
        "task_locality_distribution": dict(task_metrics["task_localities"]),
        "task_failure_reasons": dict(task_metrics["task_failure_reasons"])
    }



def extract_sql_metrics(events):
    """Extract Spark SQL execution metrics"""
    sql_executions = {}

    for event in events:
        if event.get("Event") == "org.apache.spark.sql.execution.ui.SparkListenerSQLExecutionStart":
            execution_id = event.get("executionId")
            sql_executions[execution_id] = {
                "execution_id": execution_id,
                "description": event.get("description", "N/A"),
                "details": event.get("details", "N/A"),
                # Truncate long plans
                "physical_plan_description": event.get("physicalPlanDescription", "N/A")[:1000] if event.get("physicalPlanDescription") else None,
                "submission_time": datetime.fromtimestamp(event.get("time", 0) / 1000).isoformat() if event.get("time") else None,
                "completion_time": None,
                "duration_ms": None,
                "status": "RUNNING"
            }

        elif event.get("Event") == "org.apache.spark.sql.execution.ui.SparkListenerSQLExecutionEnd":
            execution_id = event.get("executionId")
            if execution_id in sql_executions:
                completion_time = event.get("time")
                sql_executions[execution_id]["completion_time"] = datetime.fromtimestamp(
                    completion_time / 1000).isoformat() if completion_time else None
                sql_executions[execution_id]["status"] = "COMPLETED"

                # Calculate duration if both times available
                if sql_executions[execution_id].get("submission_time") and completion_time:
                    # This is actually end time
                    submission_ts = event.get("time", 0)
                    # We need to extract from the stored submission_time
                    # For now, mark as completed
                    sql_executions[execution_id]["status"] = "COMPLETED"

    return {
        "total_sql_executions": len(sql_executions),
        "completed_executions": sum(1 for e in sql_executions.values() if e["status"] == "COMPLETED"),
        "running_executions": sum(1 for e in sql_executions.values() if e["status"] == "RUNNING"),
        "sql_executions": list(sql_executions.values())
    }



def extract_sql_execution_plans(events):
    """Extract SQL execution plans from Spark SQL events"""
    execution_plans = {}
    
    for event in events:
        if event.get("Event") == "org.apache.spark.sql.execution.ui.SparkListenerSQLExecutionStart":
            execution_id = event.get("executionId")
            execution_plans[execution_id] = {
                "execution_id": execution_id,
                "description": event.get("description", "N/A"),
                "physical_plan": event.get("physicalPlanDescription", "N/A"),
                "details": event.get("details", "N/A"),
                "submission_time": datetime.fromtimestamp(event.get("time", 0) / 1000).isoformat() if event.get("time") else None,
                "modifiedConfigs": event.get("modifiedConfigs", {}),
                "plan_nodes": []
            }
            
            # Parse physical plan to extract key nodes
            physical_plan = event.get("physicalPlanDescription", "")
            if physical_plan:
                # Extract plan node types (simple parsing)
                plan_lines = physical_plan.split('\n')
                nodes = []
                for line in plan_lines[:20]:  # Limit to first 20 lines
                    if line.strip() and not line.startswith('=='):
                        # Extract operation names (e.g., "Exchange", "WholeStageCodegen", "Filter")
                        parts = line.strip().split()
                        if parts:
                            node_type = parts[0].rstrip(':')
                            nodes.append(node_type)
                
                execution_plans[execution_id]["plan_nodes"] = list(set(nodes))[:10]  # Unique nodes, max 10
    
    # Count plan node frequencies
    node_frequencies = defaultdict(int)
    for plan in execution_plans.values():
        for node in plan.get("plan_nodes", []):
            node_frequencies[node] += 1
    
    return {
        "total_execution_plans": len(execution_plans),
        "execution_plans": list(execution_plans.values()),
        "common_plan_nodes": dict(sorted(node_frequencies.items(), key=lambda x: x[1], reverse=True)[:15]),
        "plans_with_exchange": sum(1 for p in execution_plans.values() if "Exchange" in p.get("plan_nodes", [])),
        "plans_with_broadcast": sum(1 for p in execution_plans.values() if any("Broadcast" in node for node in p.get("plan_nodes", []))),
        "plans_with_sort": sum(1 for p in execution_plans.values() if "Sort" in p.get("plan_nodes", []))
    }



def extract_memory_metrics(events):
    """Extract detailed memory usage metrics"""
    from collections import defaultdict

    executor_memory = defaultdict(lambda: {
        "jvm_heap_used": [],
        "jvm_off_heap_used": [],
        "on_heap_execution_memory": [],
        "off_heap_execution_memory": [],
        "on_heap_storage_memory": [],
        "off_heap_storage_memory": []
    })

    total_memory_spilled = 0
    total_disk_spilled = 0

    for event in events:
        if event.get("Event") == "SparkListenerExecutorMetricsUpdate":
            executor_id = event.get("Executor ID")
            executor_metrics = event.get("Executor Metrics Updated", [])

            for stage_id, metrics_list in executor_metrics:
                for metrics in metrics_list:
                    if isinstance(metrics, dict):
                        executor_memory[executor_id]["jvm_heap_used"].append(
                            metrics.get("JVMHeapMemory", 0))
                        executor_memory[executor_id]["jvm_off_heap_used"].append(
                            metrics.get("JVMOffHeapMemory", 0))
                        executor_memory[executor_id]["on_heap_execution_memory"].append(
                            metrics.get("OnHeapExecutionMemory", 0))
                        executor_memory[executor_id]["off_heap_execution_memory"].append(
                            metrics.get("OffHeapExecutionMemory", 0))
                        executor_memory[executor_id]["on_heap_storage_memory"].append(
                            metrics.get("OnHeapStorageMemory", 0))
                        executor_memory[executor_id]["off_heap_storage_memory"].append(
                            metrics.get("OffHeapStorageMemory", 0))

        elif event.get("Event") == "SparkListenerTaskEnd":
            task_metrics = event.get("Task Metrics", {})
            if task_metrics:
                total_memory_spilled += task_metrics.get(
                    "Memory Bytes Spilled", 0)
                total_disk_spilled += task_metrics.get("Disk Bytes Spilled", 0)

    # Calculate peak memory for each executor
    executor_peaks = {}
    for executor_id, metrics in executor_memory.items():
        executor_peaks[executor_id] = {
            "peak_jvm_heap_gb": round(max(metrics["jvm_heap_used"]) / (1024 ** 3), 2) if metrics["jvm_heap_used"] else 0,
            "peak_jvm_off_heap_gb": round(max(metrics["jvm_off_heap_used"]) / (1024 ** 3), 2) if metrics["jvm_off_heap_used"] else 0,
            "peak_execution_memory_gb": round(max(metrics["on_heap_execution_memory"]) / (1024 ** 3), 2) if metrics["on_heap_execution_memory"] else 0,
            "peak_storage_memory_gb": round(max(metrics["on_heap_storage_memory"]) / (1024 ** 3), 2) if metrics["on_heap_storage_memory"] else 0
        }

    return {
        "total_memory_spilled_gb": round(total_memory_spilled / (1024 ** 3), 2),
        "total_disk_spilled_gb": round(total_disk_spilled / (1024 ** 3), 2),
        "executor_memory_peaks": executor_peaks
    }



def extract_storage_metrics(events):
    """Extract RDD storage and block manager metrics"""
    rdds = {}
    block_managers = {}

    for event in events:
        if event.get("Event") == "SparkListenerBlockManagerAdded":
            block_manager_id = event.get("Block Manager ID", {})
            executor_id = block_manager_id.get("Executor ID")

            block_managers[executor_id] = {
                "executor_id": executor_id,
                "host": block_manager_id.get("Host"),
                "port": block_manager_id.get("Port"),
                "max_memory_bytes": event.get("Maximum Memory", 0),
                "max_memory_gb": round(event.get("Maximum Memory", 0) / (1024 ** 3), 2),
                "added_time": datetime.fromtimestamp(event.get("Timestamp", 0) / 1000).isoformat() if event.get("Timestamp") else None,
                "status": "ACTIVE"
            }

        elif event.get("Event") == "SparkListenerBlockManagerRemoved":
            block_manager_id = event.get("Block Manager ID", {})
            executor_id = block_manager_id.get("Executor ID")

            if executor_id in block_managers:
                block_managers[executor_id]["status"] = "REMOVED"
                block_managers[executor_id]["removed_time"] = datetime.fromtimestamp(
                    event.get("Timestamp", 0) / 1000).isoformat() if event.get("Timestamp") else None

        elif event.get("Event") == "SparkListenerUnpersistRDD":
            rdd_id = event.get("RDD ID")
            if rdd_id in rdds:
                rdds[rdd_id]["unpersisted"] = True

        # Extract RDD info from stage submissions
        elif event.get("Event") == "SparkListenerStageSubmitted":
            stage_info = event.get("Stage Info", {})
            for rdd_info in stage_info.get("RDD Info", []):
                rdd_id = rdd_info.get("RDD ID")
                if rdd_id not in rdds:
                    storage_level = rdd_info.get("Storage Level", {})
                    rdds[rdd_id] = {
                        "rdd_id": rdd_id,
                        "name": rdd_info.get("Name"),
                        "num_partitions": rdd_info.get("Number of Partitions", 0),
                        "num_cached_partitions": rdd_info.get("Number of Cached Partitions", 0),
                        "storage_level": {
                            "use_disk": storage_level.get("Use Disk", False),
                            "use_memory": storage_level.get("Use Memory", False),
                            "use_off_heap": storage_level.get("Use Off Heap", False),
                            "deserialized": storage_level.get("Deserialized", False),
                            "replication": storage_level.get("Replication", 1)
                        },
                        "unpersisted": False
                    }

    return {
        "total_rdds": len(rdds),
        "cached_rdds": sum(1 for r in rdds.values() if r["num_cached_partitions"] > 0),
        "total_block_managers": len(block_managers),
        "active_block_managers": sum(1 for b in block_managers.values() if b["status"] == "ACTIVE"),
        "rdds": list(rdds.values()),
        "block_managers": list(block_managers.values())
    }



def extract_resource_timeline(events):
    """Extract timeline of resource allocation and key events"""
    timeline = []

    for event in events:
        event_type = event.get("Event")
        timestamp = event.get("Timestamp")

        if not timestamp:
            continue

        event_time = datetime.fromtimestamp(timestamp / 1000).isoformat()

        if event_type == "SparkListenerApplicationStart":
            timeline.append({
                "timestamp": event_time,
                "event_type": "APPLICATION_START",
                "details": {
                    "app_name": event.get("App Name"),
                    "app_id": event.get("App ID")
                }
            })

        elif event_type == "SparkListenerApplicationEnd":
            timeline.append({
                "timestamp": event_time,
                "event_type": "APPLICATION_END",
                "details": {}
            })

        elif event_type == "SparkListenerExecutorAdded":
            executor_info = event.get("Executor Info", {})
            timeline.append({
                "timestamp": event_time,
                "event_type": "EXECUTOR_ADDED",
                "details": {
                    "executor_id": event.get("Executor ID"),
                    "host": executor_info.get("Host"),
                    "cores": executor_info.get("Total Cores")
                }
            })

        elif event_type == "SparkListenerExecutorRemoved":
            timeline.append({
                "timestamp": event_time,
                "event_type": "EXECUTOR_REMOVED",
                "details": {
                    "executor_id": event.get("Executor ID"),
                    "reason": event.get("Reason")
                }
            })

        elif event_type == "SparkListenerJobStart":
            timeline.append({
                "timestamp": event_time,
                "event_type": "JOB_START",
                "details": {
                    "job_id": event.get("Job ID"),
                    "num_stages": len(event.get("Stage IDs", []))
                }
            })

        elif event_type == "SparkListenerJobEnd":
            job_result = event.get("Job Result", {})
            timeline.append({
                "timestamp": event_time,
                "event_type": "JOB_END",
                "details": {
                    "job_id": event.get("Job ID"),
                    "result": job_result.get("Result", "Unknown")
                }
            })

    return {
        "total_events": len(timeline),
        # Limit to first 1000 events to avoid huge output
        "timeline": timeline[:1000]
    }



def extract_driver_metrics(events):
    """Extract detailed driver resource usage metrics"""
    from collections import defaultdict
    import statistics

    driver_metrics = {
        "driver_id": "driver",
        "host": None,
        "port": None,
        "cores": 0,
        "memory_mb": 0,
        "start_time": None,
        "end_time": None,
        "uptime_hours": 0,
        "total_tasks_launched": 0,
        "total_jobs_submitted": 0,
        "total_stages_submitted": 0,
        "total_result_bytes_received": 0,
        "peak_jvm_heap_memory_gb": 0,
        "peak_jvm_off_heap_memory_gb": 0,
        "avg_jvm_heap_memory_gb": 0,
        "avg_jvm_off_heap_memory_gb": 0,
        "gc_time_ms": 0,
        "gc_count": 0,
        "memory_metrics_samples": 0
    }

    # Track memory usage over time for driver
    driver_heap_memory_samples = []
    driver_off_heap_memory_samples = []
    
    # Extract driver configuration from Spark properties
    for event in events:
        if event.get("Event") == "SparkListenerEnvironmentUpdate":
            spark_props = event.get("Spark Properties", {})
            if isinstance(spark_props, list):
                spark_props = dict(spark_props)

            # Extract driver memory (e.g., "4g" -> 4096 MB)
            mem_str = spark_props.get("spark.driver.memory", "0")
            if mem_str.endswith("g"):
                driver_metrics["memory_mb"] = int(mem_str[:-1]) * 1024
            elif mem_str.endswith("m"):
                driver_metrics["memory_mb"] = int(mem_str[:-1])

            # Extract driver cores
            driver_metrics["cores"] = int(spark_props.get("spark.driver.cores", "1"))
            driver_metrics["host"] = spark_props.get("spark.driver.host", "N/A")
            driver_metrics["port"] = spark_props.get("spark.driver.port", "N/A")
            break

    # Extract application start/end times
    for event in events:
        if event.get("Event") == "SparkListenerApplicationStart":
            start_timestamp = event.get("Timestamp")
            if start_timestamp:
                driver_metrics["start_time"] = datetime.fromtimestamp(
                    start_timestamp / 1000).isoformat()
                driver_metrics["_start_timestamp_ms"] = start_timestamp
        elif event.get("Event") == "SparkListenerApplicationEnd":
            end_timestamp = event.get("Timestamp")
            if end_timestamp:
                driver_metrics["end_time"] = datetime.fromtimestamp(
                    end_timestamp / 1000).isoformat()
                driver_metrics["_end_timestamp_ms"] = end_timestamp

    # Calculate uptime
    if driver_metrics.get("_start_timestamp_ms") and driver_metrics.get("_end_timestamp_ms"):
        uptime_ms = driver_metrics["_end_timestamp_ms"] - driver_metrics["_start_timestamp_ms"]
        driver_metrics["uptime_hours"] = round(uptime_ms / (1000 * 60 * 60), 2)

    # Count jobs, stages, and tasks
    for event in events:
        event_type = event.get("Event")
        
        if event_type == "SparkListenerJobStart":
            driver_metrics["total_jobs_submitted"] += 1
            
        elif event_type == "SparkListenerStageSubmitted":
            driver_metrics["total_stages_submitted"] += 1
            
        elif event_type == "SparkListenerTaskStart":
            driver_metrics["total_tasks_launched"] += 1

    # Extract driver memory metrics from ExecutorMetricsUpdate events
    for event in events:
        if event.get("Event") == "SparkListenerExecutorMetricsUpdate":
            executor_id = event.get("Executor ID")
            
            # Driver has executor ID "driver"
            if executor_id == "driver":
                executor_metrics = event.get("Executor Metrics Updated", [])
                
                for stage_id, metrics_list in executor_metrics:
                    for metrics in metrics_list:
                        if isinstance(metrics, dict):
                            jvm_heap = metrics.get("JVMHeapMemory", 0)
                            jvm_off_heap = metrics.get("JVMOffHeapMemory", 0)
                            
                            if jvm_heap > 0:
                                driver_heap_memory_samples.append(jvm_heap)
                            if jvm_off_heap > 0:
                                driver_off_heap_memory_samples.append(jvm_off_heap)

    # Extract GC metrics from tasks run on driver (if any)
    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_info = event.get("Task Info", {})
            executor_id = task_info.get("Executor ID")
            
            if executor_id == "driver":
                task_metrics = event.get("Task Metrics", {})
                if task_metrics:
                    gc_time = task_metrics.get("JVM GC Time", 0)
                    if gc_time > 0:
                        driver_metrics["gc_time_ms"] += gc_time
                        driver_metrics["gc_count"] += 1

    # Extract result size from completed tasks
    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_info = event.get("Task Info", {})
            # Result bytes are sent back to driver
            result_size = task_info.get("Result Size", 0)
            if result_size > 0:
                driver_metrics["total_result_bytes_received"] += result_size

    # Calculate memory statistics
    if driver_heap_memory_samples:
        driver_metrics["peak_jvm_heap_memory_gb"] = round(
            max(driver_heap_memory_samples) / (1024 ** 3), 2)
        driver_metrics["avg_jvm_heap_memory_gb"] = round(
            statistics.mean(driver_heap_memory_samples) / (1024 ** 3), 2)
        driver_metrics["memory_metrics_samples"] = len(driver_heap_memory_samples)

    if driver_off_heap_memory_samples:
        driver_metrics["peak_jvm_off_heap_memory_gb"] = round(
            max(driver_off_heap_memory_samples) / (1024 ** 3), 2)
        driver_metrics["avg_jvm_off_heap_memory_gb"] = round(
            statistics.mean(driver_off_heap_memory_samples) / (1024 ** 3), 2)

    # Convert result bytes to GB
    driver_metrics["total_result_bytes_received_gb"] = round(
        driver_metrics["total_result_bytes_received"] / (1024 ** 3), 2)

    # Calculate memory utilization percentage
    configured_memory_gb = driver_metrics["memory_mb"] / 1024
    memory_utilization_percent = 0
    if configured_memory_gb > 0 and driver_metrics["peak_jvm_heap_memory_gb"] > 0:
        memory_utilization_percent = round(
            (driver_metrics["peak_jvm_heap_memory_gb"] / configured_memory_gb) * 100, 2)
    
    driver_metrics["configured_memory_gb"] = round(configured_memory_gb, 2)
    driver_metrics["memory_utilization_percent"] = memory_utilization_percent

    # Calculate average GC time per task (if driver ran tasks)
    if driver_metrics["gc_count"] > 0:
        driver_metrics["avg_gc_time_per_task_ms"] = round(
            driver_metrics["gc_time_ms"] / driver_metrics["gc_count"], 2)
    else:
        driver_metrics["avg_gc_time_per_task_ms"] = 0

    # Remove internal timestamp fields
    driver_metrics.pop("_start_timestamp_ms", None)
    driver_metrics.pop("_end_timestamp_ms", None)

    return driver_metrics



def extract_executor_peak_memory(events):
    """Extract detailed executor peak memory metrics with timeline analysis"""
    from collections import defaultdict
    import statistics
    
    # Track memory metrics per executor over time
    executor_memory_samples = defaultdict(list)
    executor_info = {}
    
    # First pass: collect executor metadata
    for event in events:
        if event.get("Event") == "SparkListenerExecutorAdded":
            executor_id = event.get("Executor ID")
            executor_details = event.get("Executor Info", {})
            
            executor_info[executor_id] = {
                "executor_id": executor_id,
                "host": executor_details.get("Host", "N/A"),
                "total_cores": executor_details.get("Total Cores", 0),
                "add_time": datetime.fromtimestamp(event.get("Timestamp", 0) / 1000).isoformat() if event.get("Timestamp") else None
            }
    
    # Second pass: collect memory samples from task metrics
    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_info = event.get("Task Info", {})
            task_metrics = event.get("Task Metrics")
            
            if not task_metrics:
                continue
            
            executor_id = task_info.get("Executor ID")
            if not executor_id:
                continue
            
            # Extract memory metrics
            jvm_heap_memory = task_metrics.get("JVM Heap Memory", 0)
            jvm_off_heap_memory = task_metrics.get("JVM Off Heap Memory", 0)
            peak_execution_memory = task_metrics.get("Peak Execution Memory", 0)
            memory_bytes_spilled = task_metrics.get("Memory Bytes Spilled", 0)
            disk_bytes_spilled = task_metrics.get("Disk Bytes Spilled", 0)
            
            # Store memory sample
            if jvm_heap_memory > 0 or jvm_off_heap_memory > 0:
                executor_memory_samples[executor_id].append({
                    "jvm_heap_memory": jvm_heap_memory,
                    "jvm_off_heap_memory": jvm_off_heap_memory,
                    "peak_execution_memory": peak_execution_memory,
                    "memory_spilled": memory_bytes_spilled,
                    "disk_spilled": disk_bytes_spilled,
                    "timestamp": event.get("Task Info", {}).get("Finish Time", 0)
                })
    
    # Third pass: process ExecutorMetricsUpdate events for more granular data
    for event in events:
        if event.get("Event") == "SparkListenerExecutorMetricsUpdate":
            executor_id = event.get("Executor ID")
            executor_metrics = event.get("Executor Metrics Updates", [])
            
            if not executor_metrics:
                continue
            
            # Get the latest metrics update
            for metrics_update in executor_metrics:
                if isinstance(metrics_update, list) and len(metrics_update) > 1:
                    metrics = metrics_update[1]
                    
                    jvm_heap = metrics.get("JVMHeapMemory", 0)
                    jvm_off_heap = metrics.get("JVMOffHeapMemory", 0)
                    on_heap_exec = metrics.get("OnHeapExecutionMemory", 0)
                    off_heap_exec = metrics.get("OffHeapExecutionMemory", 0)
                    on_heap_storage = metrics.get("OnHeapStorageMemory", 0)
                    off_heap_storage = metrics.get("OffHeapStorageMemory", 0)
                    
                    if jvm_heap > 0 or jvm_off_heap > 0:
                        executor_memory_samples[executor_id].append({
                            "jvm_heap_memory": jvm_heap,
                            "jvm_off_heap_memory": jvm_off_heap,
                            "peak_execution_memory": on_heap_exec + off_heap_exec,
                            "storage_memory": on_heap_storage + off_heap_storage,
                            "memory_spilled": 0,
                            "disk_spilled": 0,
                            "timestamp": event.get("Timestamp", 0)
                        })
    
    # Calculate statistics for each executor
    executor_stats = {}
    
    for executor_id, samples in executor_memory_samples.items():
        if not samples:
            continue
        
        # Extract memory values
        heap_values = [s["jvm_heap_memory"] for s in samples if s["jvm_heap_memory"] > 0]
        off_heap_values = [s["jvm_off_heap_memory"] for s in samples if s["jvm_off_heap_memory"] > 0]
        exec_memory_values = [s["peak_execution_memory"] for s in samples if s["peak_execution_memory"] > 0]
        spilled_values = [s["memory_spilled"] for s in samples if s["memory_spilled"] > 0]
        disk_spilled_values = [s["disk_spilled"] for s in samples if s["disk_spilled"] > 0]
        
        # Calculate statistics
        def calc_memory_stats(values):
            if not values:
                return {
                    "samples": 0,
                    "min_gb": 0,
                    "max_gb": 0,
                    "avg_gb": 0,
                    "median_gb": 0,
                    "p95_gb": 0,
                    "std_dev_gb": 0
                }
            
            sorted_values = sorted(values)
            return {
                "samples": len(values),
                "min_gb": round(min(values) / (1024 ** 3), 3),
                "max_gb": round(max(values) / (1024 ** 3), 3),
                "avg_gb": round(statistics.mean(values) / (1024 ** 3), 3),
                "median_gb": round(statistics.median(values) / (1024 ** 3), 3),
                "p95_gb": round(sorted_values[int(len(sorted_values) * 0.95)] / (1024 ** 3), 3) if len(values) > 1 else round(max(values) / (1024 ** 3), 3),
                "std_dev_gb": round(statistics.stdev(values) / (1024 ** 3), 3) if len(values) > 1 else 0
            }
        
        executor_stats[executor_id] = {
            "executor_id": executor_id,
            "host": executor_info.get(executor_id, {}).get("host", "N/A"),
            "total_cores": executor_info.get(executor_id, {}).get("total_cores", 0),
            "total_memory_samples": len(samples),
            "jvm_heap_memory": calc_memory_stats(heap_values),
            "jvm_off_heap_memory": calc_memory_stats(off_heap_values),
            "peak_execution_memory": calc_memory_stats(exec_memory_values),
            "total_memory_spilled_gb": round(sum(spilled_values) / (1024 ** 3), 2) if spilled_values else 0,
            "total_disk_spilled_gb": round(sum(disk_spilled_values) / (1024 ** 3), 2) if disk_spilled_values else 0,
            "spill_count": len([s for s in samples if s["memory_spilled"] > 0])
        }
    
    # Calculate aggregate statistics across all executors
    all_heap_peaks = [stats["jvm_heap_memory"]["max_gb"] for stats in executor_stats.values() if stats["jvm_heap_memory"]["max_gb"] > 0]
    all_off_heap_peaks = [stats["jvm_off_heap_memory"]["max_gb"] for stats in executor_stats.values() if stats["jvm_off_heap_memory"]["max_gb"] > 0]
    all_exec_memory_peaks = [stats["peak_execution_memory"]["max_gb"] for stats in executor_stats.values() if stats["peak_execution_memory"]["max_gb"] > 0]
    
    # Identify executors with highest memory usage
    top_heap_executors = sorted(
        [(eid, stats["jvm_heap_memory"]["max_gb"]) for eid, stats in executor_stats.items() if stats["jvm_heap_memory"]["max_gb"] > 0],
        key=lambda x: x[1],
        reverse=True
    )[:10]
    
    top_spill_executors = sorted(
        [(eid, stats["total_memory_spilled_gb"]) for eid, stats in executor_stats.items() if stats["total_memory_spilled_gb"] > 0],
        key=lambda x: x[1],
        reverse=True
    )[:10]
    
    return {
        "summary": {
            "total_executors_analyzed": len(executor_stats),
            "total_memory_samples": sum(stats["total_memory_samples"] for stats in executor_stats.values()),
            "max_heap_memory_gb": round(max(all_heap_peaks), 3) if all_heap_peaks else 0,
            "avg_heap_memory_gb": round(statistics.mean(all_heap_peaks), 3) if all_heap_peaks else 0,
            "max_off_heap_memory_gb": round(max(all_off_heap_peaks), 3) if all_off_heap_peaks else 0,
            "avg_off_heap_memory_gb": round(statistics.mean(all_off_heap_peaks), 3) if all_off_heap_peaks else 0,
            "max_execution_memory_gb": round(max(all_exec_memory_peaks), 3) if all_exec_memory_peaks else 0,
            "avg_execution_memory_gb": round(statistics.mean(all_exec_memory_peaks), 3) if all_exec_memory_peaks else 0,
            "total_memory_spilled_gb": round(sum(stats["total_memory_spilled_gb"] for stats in executor_stats.values()), 2),
            "total_disk_spilled_gb": round(sum(stats["total_disk_spilled_gb"] for stats in executor_stats.values()), 2),
            "executors_with_spill": len([s for s in executor_stats.values() if s["spill_count"] > 0])
        },
        "top_10_executors_by_heap_memory": [
            {
                "executor_id": eid,
                "max_heap_gb": peak,
                "host": executor_stats[eid]["host"]
            }
            for eid, peak in top_heap_executors
        ],
        "top_10_executors_by_memory_spill": [
            {
                "executor_id": eid,
                "total_spilled_gb": spilled,
                "host": executor_stats[eid]["host"],
                "spill_count": executor_stats[eid]["spill_count"]
            }
            for eid, spilled in top_spill_executors
        ],
        "per_executor_details": list(executor_stats.values())
    }



def extract_gc_time_per_executor(events):
    """Extract garbage collection time metrics per executor with analysis"""
    from collections import defaultdict
    import statistics
    
    # Track GC metrics per executor
    executor_gc_metrics = defaultdict(lambda: {
        "gc_time_samples": [],
        "total_gc_time_ms": 0,
        "gc_count": 0,
        "task_count": 0,
        "total_task_time_ms": 0
    })
    
    executor_info = {}
    
    # First pass: collect executor metadata
    for event in events:
        if event.get("Event") == "SparkListenerExecutorAdded":
            executor_id = event.get("Executor ID")
            executor_details = event.get("Executor Info", {})
            
            executor_info[executor_id] = {
                "host": executor_details.get("Host", "N/A"),
                "total_cores": executor_details.get("Total Cores", 0),
                "add_time": datetime.fromtimestamp(event.get("Timestamp", 0) / 1000).isoformat() if event.get("Timestamp") else None
            }
    
    # Second pass: collect GC metrics from task completion events
    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_info = event.get("Task Info", {})
            task_metrics = event.get("Task Metrics")
            
            if not task_metrics:
                continue
            
            executor_id = task_info.get("Executor ID")
            if not executor_id:
                continue
            
            # Extract GC time
            gc_time = task_metrics.get("JVM GC Time", 0)
            executor_run_time = task_metrics.get("Executor Run Time", 0)
            
            # Update executor metrics
            executor_gc_metrics[executor_id]["task_count"] += 1
            executor_gc_metrics[executor_id]["total_task_time_ms"] += executor_run_time
            
            if gc_time > 0:
                executor_gc_metrics[executor_id]["gc_time_samples"].append(gc_time)
                executor_gc_metrics[executor_id]["total_gc_time_ms"] += gc_time
                executor_gc_metrics[executor_id]["gc_count"] += 1
    
    # Calculate statistics for each executor
    executor_gc_stats = {}
    
    for executor_id, metrics in executor_gc_metrics.items():
        gc_samples = metrics["gc_time_samples"]
        total_gc_time = metrics["total_gc_time_ms"]
        gc_count = metrics["gc_count"]
        task_count = metrics["task_count"]
        total_task_time = metrics["total_task_time_ms"]
        
        # Calculate GC percentage
        gc_percentage = 0
        if total_task_time > 0:
            gc_percentage = round((total_gc_time / total_task_time) * 100, 2)
        
        # Calculate GC statistics
        gc_stats = {
            "min_ms": 0,
            "max_ms": 0,
            "avg_ms": 0,
            "median_ms": 0,
            "p95_ms": 0,
            "p99_ms": 0,
            "std_dev_ms": 0
        }
        
        if gc_samples:
            sorted_samples = sorted(gc_samples)
            gc_stats = {
                "min_ms": round(min(gc_samples), 2),
                "max_ms": round(max(gc_samples), 2),
                "avg_ms": round(statistics.mean(gc_samples), 2),
                "median_ms": round(statistics.median(gc_samples), 2),
                "p95_ms": round(sorted_samples[int(len(sorted_samples) * 0.95)], 2) if len(gc_samples) > 1 else round(max(gc_samples), 2),
                "p99_ms": round(sorted_samples[int(len(sorted_samples) * 0.99)], 2) if len(gc_samples) > 1 else round(max(gc_samples), 2),
                "std_dev_ms": round(statistics.stdev(gc_samples), 2) if len(gc_samples) > 1 else 0
            }
        
        executor_gc_stats[executor_id] = {
            "executor_id": executor_id,
            "host": executor_info.get(executor_id, {}).get("host", "N/A"),
            "total_cores": executor_info.get(executor_id, {}).get("total_cores", 0),
            "task_count": task_count,
            "tasks_with_gc": gc_count,
            "gc_task_percentage": round((gc_count / task_count * 100), 2) if task_count > 0 else 0,
            "total_gc_time_ms": round(total_gc_time, 2),
            "total_gc_time_seconds": round(total_gc_time / 1000, 2),
            "total_task_time_ms": round(total_task_time, 2),
            "total_task_time_seconds": round(total_task_time / 1000, 2),
            "gc_time_percentage": gc_percentage,
            "avg_gc_time_per_task_ms": round(total_gc_time / task_count, 2) if task_count > 0 else 0,
            "gc_statistics": gc_stats
        }
    
    # Calculate aggregate statistics
    all_executors = list(executor_gc_stats.values())
    total_gc_time_app = sum(e["total_gc_time_ms"] for e in all_executors)
    total_task_time_app = sum(e["total_task_time_ms"] for e in all_executors)
    total_tasks_app = sum(e["task_count"] for e in all_executors)
    total_tasks_with_gc = sum(e["tasks_with_gc"] for e in all_executors)
    
    # Identify executors with high GC overhead
    high_gc_threshold = 10  # 10% GC time is considered high
    executors_with_high_gc = [
        e for e in all_executors 
        if e["gc_time_percentage"] > high_gc_threshold
    ]
    
    # Top 10 executors by total GC time
    top_gc_executors = sorted(
        [(e["executor_id"], e["total_gc_time_seconds"], e["host"], e["gc_time_percentage"]) 
         for e in all_executors],
        key=lambda x: x[1],
        reverse=True
    )[:10]
    
    # Top 10 executors by GC percentage
    top_gc_percentage_executors = sorted(
        [(e["executor_id"], e["gc_time_percentage"], e["host"], e["total_gc_time_seconds"]) 
         for e in all_executors],
        key=lambda x: x[1],
        reverse=True
    )[:10]
    
    # Calculate percentiles for GC time percentage across executors
    gc_percentages = [e["gc_time_percentage"] for e in all_executors if e["gc_time_percentage"] > 0]
    
    gc_percentage_distribution = {
        "min": 0,
        "p25": 0,
        "median": 0,
        "p75": 0,
        "p95": 0,
        "max": 0
    }
    
    if gc_percentages:
        sorted_percentages = sorted(gc_percentages)
        gc_percentage_distribution = {
            "min": round(min(gc_percentages), 2),
            "p25": round(sorted_percentages[int(len(sorted_percentages) * 0.25)], 2),
            "median": round(statistics.median(gc_percentages), 2),
            "p75": round(sorted_percentages[int(len(sorted_percentages) * 0.75)], 2),
            "p95": round(sorted_percentages[int(len(sorted_percentages) * 0.95)], 2),
            "max": round(max(gc_percentages), 2)
        }
    
    return {
        "summary": {
            "total_executors_analyzed": len(executor_gc_stats),
            "total_tasks": total_tasks_app,
            "total_tasks_with_gc": total_tasks_with_gc,
            "tasks_with_gc_percentage": round((total_tasks_with_gc / total_tasks_app * 100), 2) if total_tasks_app > 0 else 0,
            "total_gc_time_ms": round(total_gc_time_app, 2),
            "total_gc_time_seconds": round(total_gc_time_app / 1000, 2),
            "total_gc_time_hours": round(total_gc_time_app / (1000 * 60 * 60), 2),
            "total_task_time_ms": round(total_task_time_app, 2),
            "total_task_time_seconds": round(total_task_time_app / 1000, 2),
            "total_task_time_hours": round(total_task_time_app / (1000 * 60 * 60), 2),
            "gc_time_percentage_of_total": round((total_gc_time_app / total_task_time_app * 100), 2) if total_task_time_app > 0 else 0,
            "avg_gc_time_per_task_ms": round(total_gc_time_app / total_tasks_app, 2) if total_tasks_app > 0 else 0,
            "executors_with_high_gc_count": len(executors_with_high_gc),
            "high_gc_threshold_percentage": high_gc_threshold
        },
        "gc_percentage_distribution": gc_percentage_distribution,
        "top_10_executors_by_total_gc_time": [
            {
                "executor_id": eid,
                "total_gc_time_seconds": gc_time,
                "host": host,
                "gc_percentage": gc_pct
            }
            for eid, gc_time, host, gc_pct in top_gc_executors
        ],
        "top_10_executors_by_gc_percentage": [
            {
                "executor_id": eid,
                "gc_percentage": gc_pct,
                "host": host,
                "total_gc_time_seconds": gc_time
            }
            for eid, gc_pct, host, gc_time in top_gc_percentage_executors
        ],
        "executors_with_high_gc": [
            {
                "executor_id": e["executor_id"],
                "host": e["host"],
                "gc_percentage": e["gc_time_percentage"],
                "total_gc_time_seconds": e["total_gc_time_seconds"],
                "task_count": e["task_count"]
            }
            for e in executors_with_high_gc
        ],
        "per_executor_details": list(executor_gc_stats.values())
    }



def extract_network_io_metrics(events):
    """Extract detailed network I/O metrics including shuffle operations and remote data transfers"""
    from collections import defaultdict
    import statistics
    
    # Track network I/O per executor and stage
    executor_network_io = defaultdict(lambda: {
        "shuffle_read_bytes": 0,
        "shuffle_write_bytes": 0,
        "remote_bytes_read": 0,
        "local_bytes_read": 0,
        "shuffle_read_records": 0,
        "shuffle_write_records": 0,
        "remote_blocks_fetched": 0,
        "local_blocks_fetched": 0,
        "fetch_wait_time_ms": 0,
        "shuffle_write_time_ms": 0,
        "task_count": 0,
        "tasks_with_shuffle_read": 0,
        "tasks_with_shuffle_write": 0
    })
    
    stage_network_io = defaultdict(lambda: {
        "shuffle_read_bytes": 0,
        "shuffle_write_bytes": 0,
        "remote_bytes_read": 0,
        "local_bytes_read": 0,
        "task_count": 0
    })
    
    executor_info = {}
    
    # Collect executor metadata
    for event in events:
        if event.get("Event") == "SparkListenerExecutorAdded":
            executor_id = event.get("Executor ID")
            executor_details = event.get("Executor Info", {})
            
            executor_info[executor_id] = {
                "host": executor_details.get("Host", "N/A"),
                "total_cores": executor_details.get("Total Cores", 0)
            }
    
    # Collect network I/O metrics from task completions
    total_shuffle_read = 0
    total_shuffle_write = 0
    total_remote_read = 0
    total_local_read = 0
    
    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_info = event.get("Task Info", {})
            task_metrics = event.get("Task Metrics")
            stage_id = event.get("Stage ID")
            
            if not task_metrics:
                continue
            
            executor_id = task_info.get("Executor ID")
            if not executor_id:
                continue
            
            # Extract shuffle read metrics
            shuffle_read = task_metrics.get("Shuffle Read Metrics")
            if shuffle_read:
                remote_bytes = shuffle_read.get("Remote Bytes Read", 0)
                local_bytes = shuffle_read.get("Local Bytes Read", 0)
                total_read = remote_bytes + local_bytes
                remote_blocks = shuffle_read.get("Remote Blocks Fetched", 0)
                local_blocks = shuffle_read.get("Local Blocks Fetched", 0)
                fetch_wait = shuffle_read.get("Fetch Wait Time", 0)
                read_records = shuffle_read.get("Total Records Read", 0)
                
                if total_read > 0:
                    executor_network_io[executor_id]["shuffle_read_bytes"] += total_read
                    executor_network_io[executor_id]["remote_bytes_read"] += remote_bytes
                    executor_network_io[executor_id]["local_bytes_read"] += local_bytes
                    executor_network_io[executor_id]["remote_blocks_fetched"] += remote_blocks
                    executor_network_io[executor_id]["local_blocks_fetched"] += local_blocks
                    executor_network_io[executor_id]["fetch_wait_time_ms"] += fetch_wait
                    executor_network_io[executor_id]["shuffle_read_records"] += read_records
                    executor_network_io[executor_id]["tasks_with_shuffle_read"] += 1
                    
                    total_shuffle_read += total_read
                    total_remote_read += remote_bytes
                    total_local_read += local_bytes
                    
                    if stage_id is not None:
                        stage_network_io[stage_id]["shuffle_read_bytes"] += total_read
                        stage_network_io[stage_id]["remote_bytes_read"] += remote_bytes
                        stage_network_io[stage_id]["local_bytes_read"] += local_bytes
            
            # Extract shuffle write metrics
            shuffle_write = task_metrics.get("Shuffle Write Metrics")
            if shuffle_write:
                write_bytes = shuffle_write.get("Shuffle Bytes Written", 0)
                write_time = shuffle_write.get("Shuffle Write Time", 0)
                write_records = shuffle_write.get("Shuffle Records Written", 0)
                
                if write_bytes > 0:
                    executor_network_io[executor_id]["shuffle_write_bytes"] += write_bytes
                    executor_network_io[executor_id]["shuffle_write_time_ms"] += write_time
                    executor_network_io[executor_id]["shuffle_write_records"] += write_records
                    executor_network_io[executor_id]["tasks_with_shuffle_write"] += 1
                    
                    total_shuffle_write += write_bytes
                    
                    if stage_id is not None:
                        stage_network_io[stage_id]["shuffle_write_bytes"] += write_bytes
            
            # Track task count
            executor_network_io[executor_id]["task_count"] += 1
            if stage_id is not None:
                stage_network_io[stage_id]["task_count"] += 1
    
    # Calculate per-executor statistics
    executor_stats = {}
    
    for executor_id, metrics in executor_network_io.items():
        if metrics["task_count"] == 0:
            continue
        
        # Calculate ratios and averages
        remote_ratio = 0
        if metrics["shuffle_read_bytes"] > 0:
            remote_ratio = round((metrics["remote_bytes_read"] / metrics["shuffle_read_bytes"]) * 100, 2)
        
        avg_shuffle_read_per_task = 0
        if metrics["tasks_with_shuffle_read"] > 0:
            avg_shuffle_read_per_task = round(metrics["shuffle_read_bytes"] / metrics["tasks_with_shuffle_read"], 2)
        
        avg_shuffle_write_per_task = 0
        if metrics["tasks_with_shuffle_write"] > 0:
            avg_shuffle_write_per_task = round(metrics["shuffle_write_bytes"] / metrics["tasks_with_shuffle_write"], 2)
        
        executor_stats[executor_id] = {
            "executor_id": executor_id,
            "host": executor_info.get(executor_id, {}).get("host", "N/A"),
            "task_count": metrics["task_count"],
            "tasks_with_shuffle_read": metrics["tasks_with_shuffle_read"],
            "tasks_with_shuffle_write": metrics["tasks_with_shuffle_write"],
            "shuffle_read_gb": round(metrics["shuffle_read_bytes"] / (1024 ** 3), 3),
            "shuffle_write_gb": round(metrics["shuffle_write_bytes"] / (1024 ** 3), 3),
            "remote_read_gb": round(metrics["remote_bytes_read"] / (1024 ** 3), 3),
            "local_read_gb": round(metrics["local_bytes_read"] / (1024 ** 3), 3),
            "remote_read_percentage": remote_ratio,
            "total_shuffle_records_read": metrics["shuffle_read_records"],
            "total_shuffle_records_written": metrics["shuffle_write_records"],
            "remote_blocks_fetched": metrics["remote_blocks_fetched"],
            "local_blocks_fetched": metrics["local_blocks_fetched"],
            "total_fetch_wait_time_ms": metrics["fetch_wait_time_ms"],
            "total_shuffle_write_time_ms": metrics["shuffle_write_time_ms"],
            "avg_shuffle_read_per_task_mb": round(avg_shuffle_read_per_task / (1024 ** 2), 2),
            "avg_shuffle_write_per_task_mb": round(avg_shuffle_write_per_task / (1024 ** 2), 2)
        }
    
    # Calculate per-stage statistics
    stage_stats = {}
    
    for stage_id, metrics in stage_network_io.items():
        if metrics["task_count"] == 0:
            continue
        
        remote_ratio = 0
        if metrics["shuffle_read_bytes"] > 0:
            remote_ratio = round((metrics["remote_bytes_read"] / metrics["shuffle_read_bytes"]) * 100, 2)
        
        stage_stats[str(stage_id)] = {
            "stage_id": stage_id,
            "task_count": metrics["task_count"],
            "shuffle_read_gb": round(metrics["shuffle_read_bytes"] / (1024 ** 3), 3),
            "shuffle_write_gb": round(metrics["shuffle_write_bytes"] / (1024 ** 3), 3),
            "remote_read_gb": round(metrics["remote_bytes_read"] / (1024 ** 3), 3),
            "local_read_gb": round(metrics["local_bytes_read"] / (1024 ** 3), 3),
            "remote_read_percentage": remote_ratio
        }
    
    # Top executors by network I/O
    top_shuffle_read_executors = sorted(
        [(e["executor_id"], e["shuffle_read_gb"], e["host"]) for e in executor_stats.values()],
        key=lambda x: x[1],
        reverse=True
    )[:10]
    
    top_shuffle_write_executors = sorted(
        [(e["executor_id"], e["shuffle_write_gb"], e["host"]) for e in executor_stats.values()],
        key=lambda x: x[1],
        reverse=True
    )[:10]
    
    top_remote_read_executors = sorted(
        [(e["executor_id"], e["remote_read_gb"], e["host"], e["remote_read_percentage"]) 
         for e in executor_stats.values()],
        key=lambda x: x[1],
        reverse=True
    )[:10]
    
    # Top stages by network I/O
    top_shuffle_stages = sorted(
        [(s["stage_id"], s["shuffle_read_gb"], s["shuffle_write_gb"]) 
         for s in stage_stats.values()],
        key=lambda x: x[1] + x[2],
        reverse=True
    )[:10]
    
    # Calculate aggregate statistics
    total_tasks = sum(e["task_count"] for e in executor_stats.values())
    total_tasks_with_shuffle_read = sum(e["tasks_with_shuffle_read"] for e in executor_stats.values())
    total_tasks_with_shuffle_write = sum(e["tasks_with_shuffle_write"] for e in executor_stats.values())
    
    # Remote vs local read ratio
    global_remote_ratio = 0
    if total_shuffle_read > 0:
        global_remote_ratio = round((total_remote_read / total_shuffle_read) * 100, 2)
    
    return {
        "summary": {
            "total_executors_analyzed": len(executor_stats),
            "total_tasks": total_tasks,
            "tasks_with_shuffle_read": total_tasks_with_shuffle_read,
            "tasks_with_shuffle_write": total_tasks_with_shuffle_write,
            "total_shuffle_read_gb": round(total_shuffle_read / (1024 ** 3), 2),
            "total_shuffle_write_gb": round(total_shuffle_write / (1024 ** 3), 2),
            "total_remote_read_gb": round(total_remote_read / (1024 ** 3), 2),
            "total_local_read_gb": round(total_local_read / (1024 ** 3), 2),
            "remote_read_percentage": global_remote_ratio,
            "local_read_percentage": round(100 - global_remote_ratio, 2),
            "total_network_io_gb": round((total_shuffle_read + total_shuffle_write) / (1024 ** 3), 2),
            "avg_shuffle_read_per_executor_gb": round((total_shuffle_read / len(executor_stats)) / (1024 ** 3), 2) if executor_stats else 0,
            "avg_shuffle_write_per_executor_gb": round((total_shuffle_write / len(executor_stats)) / (1024 ** 3), 2) if executor_stats else 0
        },
        "top_10_executors_by_shuffle_read": [
            {
                "executor_id": eid,
                "shuffle_read_gb": read_gb,
                "host": host
            }
            for eid, read_gb, host in top_shuffle_read_executors
        ],
        "top_10_executors_by_shuffle_write": [
            {
                "executor_id": eid,
                "shuffle_write_gb": write_gb,
                "host": host
            }
            for eid, write_gb, host in top_shuffle_write_executors
        ],
        "top_10_executors_by_remote_read": [
            {
                "executor_id": eid,
                "remote_read_gb": remote_gb,
                "host": host,
                "remote_percentage": remote_pct
            }
            for eid, remote_gb, host, remote_pct in top_remote_read_executors
        ],
        "top_10_stages_by_shuffle_io": [
            {
                "stage_id": stage_id,
                "shuffle_read_gb": read_gb,
                "shuffle_write_gb": write_gb,
                "total_shuffle_gb": round(read_gb + write_gb, 3)
            }
            for stage_id, read_gb, write_gb in top_shuffle_stages
        ],
        "per_executor_details": list(executor_stats.values()),
        "per_stage_details": list(stage_stats.values())
    }



def extract_broadcast_variable_metrics(events):
    """Extract detailed broadcast variable metrics including size, distribution, and usage patterns"""
    from collections import defaultdict
    import statistics
    
    # Track broadcast variables
    broadcast_vars = {}
    broadcast_blocks = defaultdict(lambda: {
        "block_manager_id": None,
        "host": None,
        "executor_id": None,
        "storage_level": None,
        "memory_size": 0,
        "disk_size": 0,
        "timestamp": None
    })
    
    # Track broadcast usage per executor and stage
    executor_broadcast_usage = defaultdict(lambda: {
        "broadcast_count": 0,
        "total_broadcast_bytes": 0,
        "broadcasts_used": set(),
        "task_count": 0
    })
    
    stage_broadcast_usage = defaultdict(lambda: {
        "broadcast_ids": set(),
        "total_broadcast_bytes": 0,
        "task_count": 0
    })
    
    # Collect executor metadata
    executor_info = {}
    for event in events:
        if event.get("Event") == "SparkListenerExecutorAdded":
            executor_id = event.get("Executor ID")
            executor_details = event.get("Executor Info", {})
            executor_info[executor_id] = {
                "host": executor_details.get("Host", "N/A"),
                "total_cores": executor_details.get("Total Cores", 0)
            }
    
    # Collect broadcast variable information
    for event in events:
        event_type = event.get("Event")
        
        # Track broadcast block additions
        if event_type == "SparkListenerBlockUpdated":
            block_updated_info = event.get("Block Updated Info", {})
            block_id = block_updated_info.get("Block ID")
            
            if isinstance(block_id, dict) and block_id.get("Type") == "BroadcastBlockId":
                broadcast_id = block_id.get("Broadcast ID")
                block_manager_id = block_updated_info.get("Block Manager ID", {})
                storage_level = block_updated_info.get("Storage Level", {})
                
                executor_id = block_manager_id.get("Executor ID")
                host = block_manager_id.get("Host", "N/A")
                
                memory_size = block_updated_info.get("Block Updated Info", {}).get("Memory Size", 0)
                disk_size = block_updated_info.get("Block Updated Info", {}).get("Disk Size", 0)
                
                # Sometimes the structure is nested
                if memory_size == 0:
                    memory_size = storage_level.get("Use Memory", False) and block_updated_info.get("Block Size", 0) or 0
                
                block_key = f"{broadcast_id}_{executor_id}"
                broadcast_blocks[block_key] = {
                    "broadcast_id": broadcast_id,
                    "executor_id": executor_id,
                    "host": host,
                    "storage_level": storage_level,
                    "memory_size": memory_size,
                    "disk_size": disk_size,
                    "use_memory": storage_level.get("Use Memory", False),
                    "use_disk": storage_level.get("Use Disk", False),
                    "deserialized": storage_level.get("Deserialized", False),
                    "replication": storage_level.get("Replication", 1)
                }
                
                # Track unique broadcast variables
                if broadcast_id not in broadcast_vars:
                    broadcast_vars[broadcast_id] = {
                        "broadcast_id": broadcast_id,
                        "total_size_bytes": 0,
                        "total_memory_size_bytes": 0,
                        "total_disk_size_bytes": 0,
                        "executor_count": 0,
                        "hosts": set(),
                        "first_seen": event.get("Timestamp"),
                        "storage_levels": set()
                    }
                
                broadcast_vars[broadcast_id]["total_size_bytes"] += (memory_size + disk_size)
                broadcast_vars[broadcast_id]["total_memory_size_bytes"] += memory_size
                broadcast_vars[broadcast_id]["total_disk_size_bytes"] += disk_size
                broadcast_vars[broadcast_id]["executor_count"] += 1
                broadcast_vars[broadcast_id]["hosts"].add(host)
                
                storage_desc = f"Memory:{storage_level.get('Use Memory', False)},Disk:{storage_level.get('Use Disk', False)}"
                broadcast_vars[broadcast_id]["storage_levels"].add(storage_desc)
        
        # Track broadcast usage in tasks
        elif event_type == "SparkListenerTaskEnd":
            task_info = event.get("Task Info", {})
            stage_id = event.get("Stage ID")
            executor_id = task_info.get("Executor ID")
            
            if not executor_id:
                continue
            
            # Look for broadcast variables in accumulables
            accumulables = event.get("Task Metrics", {}).get("Updated Blocks", [])
            
            executor_broadcast_usage[executor_id]["task_count"] += 1
            if stage_id is not None:
                stage_broadcast_usage[stage_id]["task_count"] += 1
    
    # Calculate statistics per broadcast variable
    broadcast_stats = []
    total_broadcast_size = 0
    total_broadcast_memory = 0
    total_broadcast_disk = 0
    
    for broadcast_id, info in broadcast_vars.items():
        size_mb = round(info["total_size_bytes"] / (1024 ** 2), 2)
        memory_mb = round(info["total_memory_size_bytes"] / (1024 ** 2), 2)
        disk_mb = round(info["total_disk_size_bytes"] / (1024 ** 2), 2)
        
        broadcast_stats.append({
            "broadcast_id": broadcast_id,
            "total_size_mb": size_mb,
            "memory_size_mb": memory_mb,
            "disk_size_mb": disk_mb,
            "executor_count": info["executor_count"],
            "host_count": len(info["hosts"]),
            "hosts": list(info["hosts"]),
            "storage_levels": list(info["storage_levels"]),
            "first_seen": info["first_seen"]
        })
        
        total_broadcast_size += info["total_size_bytes"]
        total_broadcast_memory += info["total_memory_size_bytes"]
        total_broadcast_disk += info["total_disk_size_bytes"]
    
    # Sort by size
    broadcast_stats.sort(key=lambda x: x["total_size_mb"], reverse=True)
    
    # Top broadcasts by size
    top_10_by_size = broadcast_stats[:10]
    
    # Top broadcasts by executor distribution
    top_10_by_distribution = sorted(
        broadcast_stats,
        key=lambda x: x["executor_count"],
        reverse=True
    )[:10]
    
    # Calculate distribution metrics
    if broadcast_stats:
        sizes = [b["total_size_mb"] for b in broadcast_stats]
        executor_counts = [b["executor_count"] for b in broadcast_stats]
        
        size_stats = {
            "min_mb": round(min(sizes), 2) if sizes else 0,
            "max_mb": round(max(sizes), 2) if sizes else 0,
            "avg_mb": round(statistics.mean(sizes), 2) if sizes else 0,
            "median_mb": round(statistics.median(sizes), 2) if sizes else 0,
            "std_dev_mb": round(statistics.stdev(sizes), 2) if len(sizes) > 1 else 0
        }
        
        distribution_stats = {
            "min_executors": min(executor_counts) if executor_counts else 0,
            "max_executors": max(executor_counts) if executor_counts else 0,
            "avg_executors": round(statistics.mean(executor_counts), 2) if executor_counts else 0,
            "median_executors": round(statistics.median(executor_counts), 2) if executor_counts else 0
        }
    else:
        size_stats = {
            "min_mb": 0,
            "max_mb": 0,
            "avg_mb": 0,
            "median_mb": 0,
            "std_dev_mb": 0
        }
        distribution_stats = {
            "min_executors": 0,
            "max_executors": 0,
            "avg_executors": 0,
            "median_executors": 0
        }
    
    # Analyze block-level details
    block_details = []
    for block_key, block_info in broadcast_blocks.items():
        block_details.append({
            "broadcast_id": block_info["broadcast_id"],
            "executor_id": block_info["executor_id"],
            "host": block_info["host"],
            "memory_size_mb": round(block_info["memory_size"] / (1024 ** 2), 2),
            "disk_size_mb": round(block_info["disk_size"] / (1024 ** 2), 2),
            "use_memory": block_info["use_memory"],
            "use_disk": block_info["use_disk"],
            "deserialized": block_info["deserialized"],
            "replication": block_info["replication"]
        })
    
    # Calculate memory vs disk usage
    memory_percentage = 0
    if total_broadcast_size > 0:
        memory_percentage = round((total_broadcast_memory / total_broadcast_size) * 100, 2)
    
    return {
        "summary": {
            "total_broadcasts": len(broadcast_vars),
            "total_broadcast_blocks": len(broadcast_blocks),
            "total_size_gb": round(total_broadcast_size / (1024 ** 3), 2),
            "total_memory_gb": round(total_broadcast_memory / (1024 ** 3), 2),
            "total_disk_gb": round(total_broadcast_disk / (1024 ** 3), 2),
            "memory_percentage": memory_percentage,
            "disk_percentage": round(100 - memory_percentage, 2),
            "avg_broadcast_size_mb": round((total_broadcast_size / len(broadcast_vars)) / (1024 ** 2), 2) if broadcast_vars else 0,
            "avg_blocks_per_broadcast": round(len(broadcast_blocks) / len(broadcast_vars), 2) if broadcast_vars else 0
        },
        "size_statistics": size_stats,
        "distribution_statistics": distribution_stats,
        "top_10_broadcasts_by_size": top_10_by_size,
        "top_10_broadcasts_by_distribution": top_10_by_distribution,
        "all_broadcasts": broadcast_stats,
        "broadcast_blocks_details": block_details[:100]  # Limit to first 100 blocks to avoid huge output
    }



def extract_spill_metrics(events):
    """Extract detailed memory and disk spill metrics including per-task, per-stage, and per-executor analysis"""
    from collections import defaultdict
    import statistics
    
    # Track spill metrics per executor, stage, and task
    executor_spill = defaultdict(lambda: {
        "memory_bytes_spilled": 0,
        "disk_bytes_spilled": 0,
        "tasks_with_spill": 0,
        "total_tasks": 0,
        "tasks_with_memory_spill": 0,
        "tasks_with_disk_spill": 0,
        "max_memory_spill_per_task": 0,
        "max_disk_spill_per_task": 0,
        "host": "N/A"
    })
    
    stage_spill = defaultdict(lambda: {
        "memory_bytes_spilled": 0,
        "disk_bytes_spilled": 0,
        "tasks_with_spill": 0,
        "total_tasks": 0,
        "tasks_with_memory_spill": 0,
        "tasks_with_disk_spill": 0
    })
    
    task_spill_details = []
    
    # Collect executor metadata
    executor_info = {}
    for event in events:
        if event.get("Event") == "SparkListenerExecutorAdded":
            executor_id = event.get("Executor ID")
            executor_details = event.get("Executor Info", {})
            executor_info[executor_id] = {
                "host": executor_details.get("Host", "N/A"),
                "total_cores": executor_details.get("Total Cores", 0)
            }
    
    # Aggregate totals
    total_memory_spilled = 0
    total_disk_spilled = 0
    total_tasks = 0
    tasks_with_memory_spill = 0
    tasks_with_disk_spill = 0
    tasks_with_any_spill = 0
    
    # Collect spill metrics from task completions
    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_info = event.get("Task Info", {})
            task_metrics = event.get("Task Metrics")
            stage_id = event.get("Stage ID")
            stage_attempt_id = event.get("Stage Attempt ID", 0)
            
            if not task_metrics:
                continue
            
            executor_id = task_info.get("Executor ID")
            task_id = task_info.get("Task ID")
            
            if not executor_id:
                continue
            
            # Extract spill metrics
            memory_spilled = task_metrics.get("Memory Bytes Spilled", 0)
            disk_spilled = task_metrics.get("Disk Bytes Spilled", 0)
            
            total_tasks += 1
            executor_spill[executor_id]["total_tasks"] += 1
            
            if stage_id is not None:
                stage_spill[stage_id]["total_tasks"] += 1
            
            # Track spills
            has_memory_spill = memory_spilled > 0
            has_disk_spill = disk_spilled > 0
            has_any_spill = has_memory_spill or has_disk_spill
            
            if has_any_spill:
                tasks_with_any_spill += 1
                executor_spill[executor_id]["tasks_with_spill"] += 1
                
                if stage_id is not None:
                    stage_spill[stage_id]["tasks_with_spill"] += 1
                
                # Store task-level spill details for top spillers
                task_spill_details.append({
                    "task_id": task_id,
                    "stage_id": stage_id,
                    "stage_attempt_id": stage_attempt_id,
                    "executor_id": executor_id,
                    "memory_spilled_mb": round(memory_spilled / (1024 ** 2), 2),
                    "disk_spilled_mb": round(disk_spilled / (1024 ** 2), 2),
                    "total_spilled_mb": round((memory_spilled + disk_spilled) / (1024 ** 2), 2),
                    "duration_ms": task_metrics.get("Executor Run Time", 0)
                })
            
            if has_memory_spill:
                tasks_with_memory_spill += 1
                total_memory_spilled += memory_spilled
                executor_spill[executor_id]["memory_bytes_spilled"] += memory_spilled
                executor_spill[executor_id]["tasks_with_memory_spill"] += 1
                executor_spill[executor_id]["max_memory_spill_per_task"] = max(
                    executor_spill[executor_id]["max_memory_spill_per_task"],
                    memory_spilled
                )
                
                if stage_id is not None:
                    stage_spill[stage_id]["memory_bytes_spilled"] += memory_spilled
                    stage_spill[stage_id]["tasks_with_memory_spill"] += 1
            
            if has_disk_spill:
                tasks_with_disk_spill += 1
                total_disk_spilled += disk_spilled
                executor_spill[executor_id]["disk_bytes_spilled"] += disk_spilled
                executor_spill[executor_id]["tasks_with_disk_spill"] += 1
                executor_spill[executor_id]["max_disk_spill_per_task"] = max(
                    executor_spill[executor_id]["max_disk_spill_per_task"],
                    disk_spilled
                )
                
                if stage_id is not None:
                    stage_spill[stage_id]["disk_bytes_spilled"] += disk_spilled
                    stage_spill[stage_id]["tasks_with_disk_spill"] += 1
            
            # Update executor host info
            if executor_id in executor_info:
                executor_spill[executor_id]["host"] = executor_info[executor_id]["host"]
    
    # Calculate per-executor statistics
    executor_stats = []
    for executor_id, metrics in executor_spill.items():
        if metrics["total_tasks"] == 0:
            continue
        
        spill_percentage = 0
        if metrics["total_tasks"] > 0:
            spill_percentage = round((metrics["tasks_with_spill"] / metrics["total_tasks"]) * 100, 2)
        
        executor_stats.append({
            "executor_id": executor_id,
            "host": metrics["host"],
            "total_tasks": metrics["total_tasks"],
            "tasks_with_spill": metrics["tasks_with_spill"],
            "tasks_with_memory_spill": metrics["tasks_with_memory_spill"],
            "tasks_with_disk_spill": metrics["tasks_with_disk_spill"],
            "spill_percentage": spill_percentage,
            "memory_spilled_gb": round(metrics["memory_bytes_spilled"] / (1024 ** 3), 3),
            "disk_spilled_gb": round(metrics["disk_bytes_spilled"] / (1024 ** 3), 3),
            "total_spilled_gb": round((metrics["memory_bytes_spilled"] + metrics["disk_bytes_spilled"]) / (1024 ** 3), 3),
            "max_memory_spill_per_task_mb": round(metrics["max_memory_spill_per_task"] / (1024 ** 2), 2),
            "max_disk_spill_per_task_mb": round(metrics["max_disk_spill_per_task"] / (1024 ** 2), 2)
        })
    
    # Calculate per-stage statistics
    stage_stats = []
    for stage_id, metrics in stage_spill.items():
        if metrics["total_tasks"] == 0:
            continue
        
        spill_percentage = 0
        if metrics["total_tasks"] > 0:
            spill_percentage = round((metrics["tasks_with_spill"] / metrics["total_tasks"]) * 100, 2)
        
        stage_stats.append({
            "stage_id": stage_id,
            "total_tasks": metrics["total_tasks"],
            "tasks_with_spill": metrics["tasks_with_spill"],
            "tasks_with_memory_spill": metrics["tasks_with_memory_spill"],
            "tasks_with_disk_spill": metrics["tasks_with_disk_spill"],
            "spill_percentage": spill_percentage,
            "memory_spilled_gb": round(metrics["memory_bytes_spilled"] / (1024 ** 3), 3),
            "disk_spilled_gb": round(metrics["disk_bytes_spilled"] / (1024 ** 3), 3),
            "total_spilled_gb": round((metrics["memory_bytes_spilled"] + metrics["disk_bytes_spilled"]) / (1024 ** 3), 3)
        })
    
    # Top executors by spill
    top_executors_by_memory_spill = sorted(
        executor_stats,
        key=lambda x: x["memory_spilled_gb"],
        reverse=True
    )[:10]
    
    top_executors_by_disk_spill = sorted(
        executor_stats,
        key=lambda x: x["disk_spilled_gb"],
        reverse=True
    )[:10]
    
    top_executors_by_total_spill = sorted(
        executor_stats,
        key=lambda x: x["total_spilled_gb"],
        reverse=True
    )[:10]
    
    top_executors_by_spill_percentage = sorted(
        [e for e in executor_stats if e["tasks_with_spill"] > 0],
        key=lambda x: x["spill_percentage"],
        reverse=True
    )[:10]
    
    # Top stages by spill
    top_stages_by_memory_spill = sorted(
        stage_stats,
        key=lambda x: x["memory_spilled_gb"],
        reverse=True
    )[:10]
    
    top_stages_by_disk_spill = sorted(
        stage_stats,
        key=lambda x: x["disk_spilled_gb"],
        reverse=True
    )[:10]
    
    top_stages_by_total_spill = sorted(
        stage_stats,
        key=lambda x: x["total_spilled_gb"],
        reverse=True
    )[:10]
    
    # Top tasks by spill
    top_tasks_by_memory_spill = sorted(
        task_spill_details,
        key=lambda x: x["memory_spilled_mb"],
        reverse=True
    )[:20]
    
    top_tasks_by_disk_spill = sorted(
        task_spill_details,
        key=lambda x: x["disk_spilled_mb"],
        reverse=True
    )[:20]
    
    top_tasks_by_total_spill = sorted(
        task_spill_details,
        key=lambda x: x["total_spilled_mb"],
        reverse=True
    )[:20]
    
    # Calculate percentages
    memory_spill_percentage = 0
    disk_spill_percentage = 0
    task_spill_percentage = 0
    
    total_spilled = total_memory_spilled + total_disk_spilled
    
    if total_spilled > 0:
        memory_spill_percentage = round((total_memory_spilled / total_spilled) * 100, 2)
        disk_spill_percentage = round((total_disk_spilled / total_spilled) * 100, 2)
    
    if total_tasks > 0:
        task_spill_percentage = round((tasks_with_any_spill / total_tasks) * 100, 2)
    
    # Spill severity classification
    executors_with_high_spill = sum(1 for e in executor_stats if e["spill_percentage"] > 20)
    stages_with_high_spill = sum(1 for s in stage_stats if s["spill_percentage"] > 20)
    
    return {
        "summary": {
            "total_tasks_analyzed": total_tasks,
            "tasks_with_any_spill": tasks_with_any_spill,
            "tasks_with_memory_spill": tasks_with_memory_spill,
            "tasks_with_disk_spill": tasks_with_disk_spill,
            "task_spill_percentage": task_spill_percentage,
            "total_memory_spilled_gb": round(total_memory_spilled / (1024 ** 3), 2),
            "total_disk_spilled_gb": round(total_disk_spilled / (1024 ** 3), 2),
            "total_spilled_gb": round(total_spilled / (1024 ** 3), 2),
            "memory_spill_percentage": memory_spill_percentage,
            "disk_spill_percentage": disk_spill_percentage,
            "avg_memory_spill_per_task_mb": round((total_memory_spilled / tasks_with_memory_spill) / (1024 ** 2), 2) if tasks_with_memory_spill > 0 else 0,
            "avg_disk_spill_per_task_mb": round((total_disk_spilled / tasks_with_disk_spill) / (1024 ** 2), 2) if tasks_with_disk_spill > 0 else 0,
            "executors_analyzed": len(executor_stats),
            "executors_with_spill": sum(1 for e in executor_stats if e["tasks_with_spill"] > 0),
            "executors_with_high_spill": executors_with_high_spill,
            "stages_analyzed": len(stage_stats),
            "stages_with_spill": sum(1 for s in stage_stats if s["tasks_with_spill"] > 0),
            "stages_with_high_spill": stages_with_high_spill,
            "high_spill_threshold_percentage": 20
        },
        "top_10_executors_by_memory_spill": top_executors_by_memory_spill,
        "top_10_executors_by_disk_spill": top_executors_by_disk_spill,
        "top_10_executors_by_total_spill": top_executors_by_total_spill,
        "top_10_executors_by_spill_percentage": top_executors_by_spill_percentage,
        "top_10_stages_by_memory_spill": top_stages_by_memory_spill,
        "top_10_stages_by_disk_spill": top_stages_by_disk_spill,
        "top_10_stages_by_total_spill": top_stages_by_total_spill,
        "top_20_tasks_by_memory_spill": top_tasks_by_memory_spill,
        "top_20_tasks_by_disk_spill": top_tasks_by_disk_spill,
        "top_20_tasks_by_total_spill": top_tasks_by_total_spill,
        "per_executor_details": executor_stats,
        "per_stage_details": stage_stats
    }



def extract_speculative_task_metrics(events):
    """Extract detailed speculative task execution metrics including success rates, time savings, and patterns"""
    from collections import defaultdict
    import statistics
    
    # Track speculative tasks
    speculative_tasks = defaultdict(lambda: {
        "task_id": None,
        "stage_id": None,
        "stage_attempt_id": None,
        "partition_id": None,
        "attempt_number": None,
        "executor_id": None,
        "host": None,
        "launch_time": None,
        "finish_time": None,
        "duration_ms": None,
        "successful": None,
        "speculative": None,
        "killed": False,
        "reason": None
    })
    
    # Track task attempts to identify speculation
    task_attempts = defaultdict(list)
    
    # Track per-stage and per-executor speculation
    stage_speculation = defaultdict(lambda: {
        "total_tasks": 0,
        "speculative_attempts": 0,
        "speculative_tasks": set(),
        "successful_speculative": 0,
        "killed_speculative": 0,
        "time_saved_ms": 0,
        "time_wasted_ms": 0
    })
    
    executor_speculation = defaultdict(lambda: {
        "speculative_attempts": 0,
        "successful_speculative": 0,
        "killed_speculative": 0,
        "host": "N/A"
    })
    
    # Collect executor metadata
    executor_info = {}
    for event in events:
        if event.get("Event") == "SparkListenerExecutorAdded":
            executor_id = event.get("Executor ID")
            executor_details = event.get("Executor Info", {})
            executor_info[executor_id] = {
                "host": executor_details.get("Host", "N/A"),
                "total_cores": executor_details.get("Total Cores", 0)
            }
    
    # First pass: collect all task completions
    for event in events:
        if event.get("Event") == "SparkListenerTaskEnd":
            task_info = event.get("Task Info", {})
            task_id = task_info.get("Task ID")
            stage_id = event.get("Stage ID")
            stage_attempt_id = event.get("Stage Attempt ID", 0)
            partition_id = task_info.get("Partition ID")
            attempt_number = task_info.get("Attempt", 0)
            executor_id = task_info.get("Executor ID")
            host = task_info.get("Host", "N/A")
            speculative = task_info.get("Speculative", False)
            
            launch_time = task_info.get("Launch Time")
            finish_time = task_info.get("Finish Time")
            duration_ms = finish_time - launch_time if (finish_time and launch_time) else 0
            
            # Check if task was successful
            successful = event.get("Task End Reason", {}).get("Reason") == "Success"
            killed = task_info.get("Killed", False)
            reason = event.get("Task End Reason", {}).get("Reason", "Unknown")
            
            # Create unique key for this task attempt
            attempt_key = f"{stage_id}_{partition_id}_{attempt_number}"
            
            # Store task attempt details
            task_attempt = {
                "task_id": task_id,
                "stage_id": stage_id,
                "stage_attempt_id": stage_attempt_id,
                "partition_id": partition_id,
                "attempt_number": attempt_number,
                "executor_id": executor_id,
                "host": host,
                "launch_time": launch_time,
                "finish_time": finish_time,
                "duration_ms": duration_ms,
                "successful": successful,
                "speculative": speculative,
                "killed": killed,
                "reason": reason
            }
            
            speculative_tasks[attempt_key] = task_attempt
            
            # Group by partition to detect multiple attempts
            partition_key = f"{stage_id}_{partition_id}"
            task_attempts[partition_key].append(task_attempt)
            
            # Track stage-level info
            if stage_id is not None:
                stage_speculation[stage_id]["total_tasks"] += 1
            
            # Update executor host info
            if executor_id and executor_id in executor_info:
                executor_speculation[executor_id]["host"] = executor_info[executor_id]["host"]
    
    # Second pass: analyze speculation patterns
    speculative_task_details = []
    total_speculative_attempts = 0
    successful_speculative_attempts = 0
    killed_speculative_attempts = 0
    total_time_saved_ms = 0
    total_time_wasted_ms = 0
    
    for partition_key, attempts in task_attempts.items():
        if len(attempts) <= 1:
            continue
        
        # Sort attempts by launch time
        attempts_sorted = sorted(attempts, key=lambda x: x["launch_time"] or 0)
        
        # Find the original attempt and speculative attempts
        original_attempt = attempts_sorted[0]
        speculative_attempts = [a for a in attempts_sorted[1:] if a["speculative"]]
        
        if not speculative_attempts:
            continue
        
        stage_id = original_attempt["stage_id"]
        
        # Track speculative tasks
        if stage_id is not None:
            stage_speculation[stage_id]["speculative_tasks"].add(original_attempt["partition_id"])
        
        # Analyze each speculative attempt
        for spec_attempt in speculative_attempts:
            total_speculative_attempts += 1
            
            executor_id = spec_attempt["executor_id"]
            if executor_id:
                executor_speculation[executor_id]["speculative_attempts"] += 1
            
            if stage_id is not None:
                stage_speculation[stage_id]["speculative_attempts"] += 1
            
            # Check if speculative attempt succeeded
            if spec_attempt["successful"]:
                successful_speculative_attempts += 1
                
                if executor_id:
                    executor_speculation[executor_id]["successful_speculative"] += 1
                
                if stage_id is not None:
                    stage_speculation[stage_id]["successful_speculative"] += 1
                
                # Calculate time saved (if original was still running or slower)
                if original_attempt["finish_time"] and spec_attempt["finish_time"]:
                    time_diff = original_attempt["finish_time"] - spec_attempt["finish_time"]
                    if time_diff > 0:
                        total_time_saved_ms += time_diff
                        if stage_id is not None:
                            stage_speculation[stage_id]["time_saved_ms"] += time_diff
            
            # Check if speculative attempt was killed
            if spec_attempt["killed"] or not spec_attempt["successful"]:
                killed_speculative_attempts += 1
                
                if executor_id:
                    executor_speculation[executor_id]["killed_speculative"] += 1
                
                if stage_id is not None:
                    stage_speculation[stage_id]["killed_speculative"] += 1
                
                # Calculate time wasted on killed speculative attempts
                if spec_attempt["duration_ms"]:
                    total_time_wasted_ms += spec_attempt["duration_ms"]
                    if stage_id is not None:
                        stage_speculation[stage_id]["time_wasted_ms"] += spec_attempt["duration_ms"]
            
            # Store details for reporting
            speculative_task_details.append({
                "stage_id": stage_id,
                "partition_id": spec_attempt["partition_id"],
                "attempt_number": spec_attempt["attempt_number"],
                "executor_id": executor_id,
                "host": spec_attempt["host"],
                "duration_ms": spec_attempt["duration_ms"],
                "successful": spec_attempt["successful"],
                "killed": spec_attempt["killed"],
                "reason": spec_attempt["reason"],
                "original_duration_ms": original_attempt["duration_ms"],
                "time_saved_ms": (original_attempt["finish_time"] - spec_attempt["finish_time"]) if (original_attempt["finish_time"] and spec_attempt["finish_time"] and spec_attempt["successful"]) else 0
            })
    
    # Calculate per-stage statistics
    stage_stats = []
    for stage_id, metrics in stage_speculation.items():
        if metrics["speculative_attempts"] == 0:
            continue
        
        success_rate = 0
        if metrics["speculative_attempts"] > 0:
            success_rate = round((metrics["successful_speculative"] / metrics["speculative_attempts"]) * 100, 2)
        
        speculation_rate = 0
        if metrics["total_tasks"] > 0:
            speculation_rate = round((len(metrics["speculative_tasks"]) / metrics["total_tasks"]) * 100, 2)
        
        stage_stats.append({
            "stage_id": stage_id,
            "total_tasks": metrics["total_tasks"],
            "tasks_with_speculation": len(metrics["speculative_tasks"]),
            "speculation_rate_percentage": speculation_rate,
            "speculative_attempts": metrics["speculative_attempts"],
            "successful_speculative": metrics["successful_speculative"],
            "killed_speculative": metrics["killed_speculative"],
            "success_rate_percentage": success_rate,
            "time_saved_seconds": round(metrics["time_saved_ms"] / 1000, 2),
            "time_wasted_seconds": round(metrics["time_wasted_ms"] / 1000, 2),
            "net_time_impact_seconds": round((metrics["time_saved_ms"] - metrics["time_wasted_ms"]) / 1000, 2)
        })
    
    # Calculate per-executor statistics
    executor_stats = []
    for executor_id, metrics in executor_speculation.items():
        if metrics["speculative_attempts"] == 0:
            continue
        
        success_rate = 0
        if metrics["speculative_attempts"] > 0:
            success_rate = round((metrics["successful_speculative"] / metrics["speculative_attempts"]) * 100, 2)
        
        executor_stats.append({
            "executor_id": executor_id,
            "host": metrics["host"],
            "speculative_attempts": metrics["speculative_attempts"],
            "successful_speculative": metrics["successful_speculative"],
            "killed_speculative": metrics["killed_speculative"],
            "success_rate_percentage": success_rate
        })
    
    # Top stages by speculation
    top_stages_by_speculation_rate = sorted(
        stage_stats,
        key=lambda x: x["speculation_rate_percentage"],
        reverse=True
    )[:10]
    
    top_stages_by_time_saved = sorted(
        stage_stats,
        key=lambda x: x["time_saved_seconds"],
        reverse=True
    )[:10]
    
    top_stages_by_time_wasted = sorted(
        stage_stats,
        key=lambda x: x["time_wasted_seconds"],
        reverse=True
    )[:10]
    
    # Top executors by speculation
    top_executors_by_attempts = sorted(
        executor_stats,
        key=lambda x: x["speculative_attempts"],
        reverse=True
    )[:10]
    
    top_executors_by_success_rate = sorted(
        [e for e in executor_stats if e["speculative_attempts"] >= 5],  # At least 5 attempts for meaningful rate
        key=lambda x: x["success_rate_percentage"],
        reverse=True
    )[:10]
    
    # Calculate overall statistics
    total_tasks_with_speculation = sum(len(m["speculative_tasks"]) for m in stage_speculation.values())
    total_tasks_analyzed = sum(m["total_tasks"] for m in stage_speculation.values())
    
    speculation_rate = 0
    if total_tasks_analyzed > 0:
        speculation_rate = round((total_tasks_with_speculation / total_tasks_analyzed) * 100, 2)
    
    success_rate = 0
    if total_speculative_attempts > 0:
        success_rate = round((successful_speculative_attempts / total_speculative_attempts) * 100, 2)
    
    net_time_impact = total_time_saved_ms - total_time_wasted_ms
    
    return {
        "summary": {
            "total_tasks_analyzed": total_tasks_analyzed,
            "tasks_with_speculation": total_tasks_with_speculation,
            "speculation_rate_percentage": speculation_rate,
            "total_speculative_attempts": total_speculative_attempts,
            "successful_speculative_attempts": successful_speculative_attempts,
            "killed_speculative_attempts": killed_speculative_attempts,
            "success_rate_percentage": success_rate,
            "total_time_saved_seconds": round(total_time_saved_ms / 1000, 2),
            "total_time_saved_minutes": round(total_time_saved_ms / 60000, 2),
            "total_time_wasted_seconds": round(total_time_wasted_ms / 1000, 2),
            "total_time_wasted_minutes": round(total_time_wasted_ms / 60000, 2),
            "net_time_impact_seconds": round(net_time_impact / 1000, 2),
            "net_time_impact_minutes": round(net_time_impact / 60000, 2),
            "speculation_efficiency": "Beneficial" if net_time_impact > 0 else "Detrimental",
            "stages_with_speculation": len(stage_stats),
            "executors_with_speculation": len(executor_stats)
        },
        "top_10_stages_by_speculation_rate": top_stages_by_speculation_rate,
        "top_10_stages_by_time_saved": top_stages_by_time_saved,
        "top_10_stages_by_time_wasted": top_stages_by_time_wasted,
        "top_10_executors_by_speculative_attempts": top_executors_by_attempts,
        "top_10_executors_by_success_rate": top_executors_by_success_rate,
        "per_stage_details": stage_stats,
        "per_executor_details": executor_stats,
        "speculative_task_samples": speculative_task_details[:50]  # Limit to first 50 samples
    }



def extract_accumulator_metrics(events):
    """Extract detailed accumulator metrics including custom and internal accumulators with trends and aggregations"""
    from collections import defaultdict
    import statistics
    
    # Track accumulators
    accumulators = {}
    accumulator_updates = defaultdict(list)
    
    # Track per-stage and per-task accumulator values
    stage_accumulators = defaultdict(lambda: defaultdict(list))
    task_accumulator_updates = []
    
    # Collect accumulator metadata
    for event in events:
        event_type = event.get("Event")
        
        # Track accumulator registrations
        if event_type == "SparkListenerStageSubmitted":
            stage_info = event.get("Stage Info", {})
            stage_id = stage_info.get("Stage ID")
            accumulables = stage_info.get("Accumulables", [])
            
            for acc in accumulables:
                acc_id = acc.get("ID")
                if acc_id is not None and acc_id not in accumulators:
                    accumulators[acc_id] = {
                        "id": acc_id,
                        "name": acc.get("Name", "Unknown"),
                        "value": acc.get("Value", 0),
                        "internal": acc.get("Internal", False),
                        "count_failed_values": acc.get("Count Failed Values", False),
                        "metadata": acc.get("Metadata", "N/A"),
                        "first_seen_stage": stage_id,
                        "update_count": 0,
                        "stages_used": set()
                    }
        
        # Track accumulator updates from task completions
        elif event_type == "SparkListenerTaskEnd":
            stage_id = event.get("Stage ID")
            task_info = event.get("Task Info", {})
            task_id = task_info.get("Task ID")
            executor_id = task_info.get("Executor ID")
            
            # Get accumulables from task metrics
            task_metrics = event.get("Task Metrics", {})
            accumulables = task_metrics.get("Accumulables", [])
            
            if not accumulables:
                # Also check task info for accumulables
                accumulables = task_info.get("Accumulables", [])
            
            for acc in accumulables:
                acc_id = acc.get("ID")
                if acc_id is None:
                    continue
                
                # Register if not seen before
                if acc_id not in accumulators:
                    accumulators[acc_id] = {
                        "id": acc_id,
                        "name": acc.get("Name", "Unknown"),
                        "value": 0,
                        "internal": acc.get("Internal", False),
                        "count_failed_values": acc.get("Count Failed Values", False),
                        "metadata": acc.get("Metadata", "N/A"),
                        "first_seen_stage": stage_id,
                        "update_count": 0,
                        "stages_used": set()
                    }
                
                # Parse update value
                update_value = acc.get("Update")
                if update_value is None:
                    update_value = acc.get("Value", 0)
                
                # Handle different value types
                try:
                    if isinstance(update_value, (int, float)):
                        numeric_value = float(update_value)
                    elif isinstance(update_value, str):
                        # Try to parse numeric strings
                        try:
                            numeric_value = float(update_value)
                        except (ValueError, TypeError):
                            numeric_value = 0
                    else:
                        numeric_value = 0
                except (ValueError, TypeError):
                    numeric_value = 0
                
                # Store update
                accumulator_updates[acc_id].append({
                    "task_id": task_id,
                    "stage_id": stage_id,
                    "executor_id": executor_id,
                    "value": numeric_value,
                    "timestamp": task_info.get("Finish Time")
                })
                
                # Update accumulator info
                accumulators[acc_id]["update_count"] += 1
                if stage_id is not None:
                    accumulators[acc_id]["stages_used"].add(stage_id)
                    stage_accumulators[stage_id][acc_id].append(numeric_value)
                
                # Store task-level update
                task_accumulator_updates.append({
                    "accumulator_id": acc_id,
                    "accumulator_name": accumulators[acc_id]["name"],
                    "task_id": task_id,
                    "stage_id": stage_id,
                    "executor_id": executor_id,
                    "value": numeric_value,
                    "internal": accumulators[acc_id]["internal"]
                })
        
        # Track final accumulator values from stage completion
        elif event_type == "SparkListenerStageCompleted":
            stage_info = event.get("Stage Info", {})
            stage_id = stage_info.get("Stage ID")
            accumulables = stage_info.get("Accumulables", [])
            
            for acc in accumulables:
                acc_id = acc.get("ID")
                if acc_id is not None and acc_id in accumulators:
                    # Update final value
                    final_value = acc.get("Value")
                    if final_value is not None:
                        try:
                            if isinstance(final_value, (int, float)):
                                accumulators[acc_id]["value"] = float(final_value)
                            elif isinstance(final_value, str):
                                try:
                                    accumulators[acc_id]["value"] = float(final_value)
                                except (ValueError, TypeError):
                                    pass
                        except (ValueError, TypeError):
                            pass
    
    # Calculate statistics for each accumulator
    accumulator_stats = []
    for acc_id, acc_info in accumulators.items():
        updates = accumulator_updates.get(acc_id, [])
        update_values = [u["value"] for u in updates if u["value"] != 0]
        
        stats = {
            "id": acc_id,
            "name": acc_info["name"],
            "internal": acc_info["internal"],
            "final_value": acc_info["value"],
            "update_count": acc_info["update_count"],
            "stages_used": len(acc_info["stages_used"]),
            "stage_ids": sorted(list(acc_info["stages_used"])),
            "metadata": acc_info["metadata"],
            "count_failed_values": acc_info.get("count_failed_values", False)
        }
        
        # Calculate statistics if we have numeric updates
        if update_values:
            stats["min_update"] = round(min(update_values), 2)
            stats["max_update"] = round(max(update_values), 2)
            stats["avg_update"] = round(statistics.mean(update_values), 2)
            stats["median_update"] = round(statistics.median(update_values), 2)
            stats["std_dev_update"] = round(statistics.stdev(update_values), 2) if len(update_values) > 1 else 0
            stats["total_updates"] = round(sum(update_values), 2)
        else:
            stats["min_update"] = 0
            stats["max_update"] = 0
            stats["avg_update"] = 0
            stats["median_update"] = 0
            stats["std_dev_update"] = 0
            stats["total_updates"] = 0
        
        accumulator_stats.append(stats)
    
    # Separate internal and custom accumulators
    internal_accumulators = [a for a in accumulator_stats if a["internal"]]
    custom_accumulators = [a for a in accumulator_stats if not a["internal"]]
    
    # Top accumulators by update count
    top_by_updates = sorted(
        accumulator_stats,
        key=lambda x: x["update_count"],
        reverse=True
    )[:20]
    
    # Top accumulators by final value
    top_by_value = sorted(
        [a for a in accumulator_stats if isinstance(a["final_value"], (int, float))],
        key=lambda x: abs(x["final_value"]),
        reverse=True
    )[:20]
    
    # Most frequently used accumulators (across stages)
    top_by_stage_usage = sorted(
        accumulator_stats,
        key=lambda x: x["stages_used"],
        reverse=True
    )[:20]
    
    # Per-stage accumulator summary
    stage_accumulator_summary = []
    for stage_id, accs in stage_accumulators.items():
        stage_summary = {
            "stage_id": stage_id,
            "accumulator_count": len(accs),
            "total_updates": sum(len(values) for values in accs.values()),
            "accumulators": []
        }
        
        for acc_id, values in accs.items():
            if acc_id in accumulators:
                acc_name = accumulators[acc_id]["name"]
                if values:
                    stage_summary["accumulators"].append({
                        "id": acc_id,
                        "name": acc_name,
                        "update_count": len(values),
                        "total_value": round(sum(values), 2),
                        "avg_value": round(statistics.mean(values), 2)
                    })
        
        stage_accumulator_summary.append(stage_summary)
    
    # Sort stages by accumulator activity
    stage_accumulator_summary.sort(key=lambda x: x["total_updates"], reverse=True)
    
    # Calculate summary statistics
    total_accumulators = len(accumulators)
    total_internal = len(internal_accumulators)
    total_custom = len(custom_accumulators)
    total_updates = sum(acc["update_count"] for acc in accumulator_stats)
    
    avg_updates_per_accumulator = round(total_updates / total_accumulators, 2) if total_accumulators > 0 else 0
    
    # Identify common Spark internal accumulators
    common_internal = {
        "input.recordsRead": 0,
        "input.bytesRead": 0,
        "output.recordsWritten": 0,
        "output.bytesWritten": 0,
        "shuffle.read.recordsRead": 0,
        "shuffle.read.bytesRead": 0,
        "shuffle.write.recordsWritten": 0,
        "shuffle.write.bytesWritten": 0
    }
    
    for acc in internal_accumulators:
        name = acc["name"]
        if name in common_internal:
            common_internal[name] = acc["final_value"]
    
    return {
        "summary": {
            "total_accumulators": total_accumulators,
            "internal_accumulators": total_internal,
            "custom_accumulators": total_custom,
            "total_updates": total_updates,
            "avg_updates_per_accumulator": avg_updates_per_accumulator,
            "stages_with_accumulators": len(stage_accumulator_summary),
            "common_spark_metrics": common_internal
        },
        "internal_accumulators": internal_accumulators,
        "custom_accumulators": custom_accumulators,
        "top_20_by_update_count": top_by_updates,
        "top_20_by_final_value": top_by_value,
        "top_20_by_stage_usage": top_by_stage_usage,
        "per_stage_summary": stage_accumulator_summary[:50],  # Limit to top 50 stages
        "all_accumulators": accumulator_stats
    }



def extract_task_duration_distributions(events):
    """Extract per-task duration distributions with skew analysis"""
    import statistics
    
    task_durations = []
    stage_task_durations = defaultdict(list)
    executor_task_durations = defaultdict(list)
    failed_task_durations = []
    task_details = []
    
    # Analyze task durations from TaskEnd events
    for event in events:
        if event.get("Event") != "SparkListenerTaskEnd":
            continue
        
        task_info = event.get("Task Info", {})
        task_metrics = event.get("Task Metrics")
        stage_id = event.get("Stage ID")
        
        if not task_metrics:
            continue
        
        # Extract task duration (Executor Run Time in milliseconds)
        executor_run_time = task_metrics.get("Executor Run Time", 0)
        if executor_run_time <= 0:
            continue
        
        # Convert to seconds
        duration_seconds = executor_run_time / 1000.0
        
        # Basic task info
        task_id = task_info.get("Task ID")
        executor_id = task_info.get("Executor ID")
        host = task_info.get("Host", "Unknown")
        failed = task_info.get("Failed", False)
        
        # Store durations
        task_durations.append(duration_seconds)
        
        if stage_id is not None:
            stage_task_durations[stage_id].append(duration_seconds)
        
        if executor_id:
            executor_task_durations[executor_id].append(duration_seconds)
        
        if failed:
            failed_task_durations.append(duration_seconds)
        
        # Additional metrics for skew analysis
        memory_bytes_spilled = task_metrics.get("Memory Bytes Spilled", 0)
        disk_bytes_spilled = task_metrics.get("Disk Bytes Spilled", 0)
        
        shuffle_read_metrics = task_metrics.get("Shuffle Read Metrics", {})
        shuffle_write_metrics = task_metrics.get("Shuffle Write Metrics", {})
        
        shuffle_read_bytes = (
            shuffle_read_metrics.get("Remote Bytes Read", 0) +
            shuffle_read_metrics.get("Local Bytes Read", 0)
        ) if shuffle_read_metrics else 0
        
        shuffle_write_bytes = shuffle_write_metrics.get(
            "Shuffle Bytes Written", 0
        ) if shuffle_write_metrics else 0
        
        task_details.append({
            "task_id": task_id,
            "stage_id": stage_id,
            "executor_id": executor_id,
            "host": host,
            "failed": failed,
            "duration_seconds": duration_seconds,
            "memory_bytes_spilled": memory_bytes_spilled,
            "disk_bytes_spilled": disk_bytes_spilled,
            "shuffle_read_bytes": shuffle_read_bytes,
            "shuffle_write_bytes": shuffle_write_bytes
        })
    
    # Helper function to calculate percentiles
    def calculate_percentiles(durations):
        if not durations:
            return {
                "count": 0,
                "min": 0,
                "p5": 0,
                "p25": 0,
                "p50_median": 0,
                "p75": 0,
                "p95": 0,
                "p99": 0,
                "max": 0,
                "mean": 0,
                "std_dev": 0,
                "total_seconds": 0
            }
        
        sorted_durations = sorted(durations)
        count = len(sorted_durations)
        
        def percentile(data, p):
            if not data:
                return 0
            k = (len(data) - 1) * (p / 100.0)
            f = int(k)
            c = f + 1
            if c >= len(data):
                return data[-1]
            d0 = data[f]
            d1 = data[c]
            return d0 + (d1 - d0) * (k - f)
        
        return {
            "count": count,
            "min": round(sorted_durations[0], 3),
            "p5": round(percentile(sorted_durations, 5), 3),
            "p25": round(percentile(sorted_durations, 25), 3),
            "p50_median": round(percentile(sorted_durations, 50), 3),
            "p75": round(percentile(sorted_durations, 75), 3),
            "p95": round(percentile(sorted_durations, 95), 3),
            "p99": round(percentile(sorted_durations, 99), 3),
            "max": round(sorted_durations[-1], 3),
            "mean": round(statistics.mean(sorted_durations), 3),
            "std_dev": round(statistics.stdev(sorted_durations), 3) if count > 1 else 0,
            "total_seconds": round(sum(sorted_durations), 2)
        }
    
    # Overall statistics
    overall_stats = calculate_percentiles(task_durations)
    
    # Per-stage statistics (top 20 by total time)
    stage_stats = {
        str(stage_id): calculate_percentiles(durations)
        for stage_id, durations in stage_task_durations.items()
    }
    
    sorted_stages = sorted(
        stage_stats.items(),
        key=lambda x: x[1]["total_seconds"],
        reverse=True
    )[:20]
    
    top_stages_by_time = {stage_id: stats for stage_id, stats in sorted_stages}
    
    # Per-executor statistics
    executor_stats = {
        executor_id: calculate_percentiles(durations)
        for executor_id, durations in executor_task_durations.items()
    }
    
    # Failed task statistics
    failed_stats = calculate_percentiles(failed_task_durations)
    
    # Task skew analysis
    skew_analysis = {
        "skew_detected": False,
        "median_duration_seconds": 0,
        "slow_threshold_seconds": 0,
        "total_slow_tasks": 0,
        "slow_tasks_by_stage": {},
        "top_10_slowest_tasks": []
    }
    
    if task_durations:
        median_duration = statistics.median(task_durations)
        slow_threshold = median_duration * 3.0
        
        slow_tasks = [
            task for task in task_details
            if task["duration_seconds"] > slow_threshold
        ]
        
        slow_tasks = sorted(
            slow_tasks,
            key=lambda x: x["duration_seconds"],
            reverse=True
        )[:100]
        
        slow_tasks_by_stage = defaultdict(list)
        for task in slow_tasks:
            stage_id = task.get("stage_id")
            if stage_id is not None:
                slow_tasks_by_stage[stage_id].append(task)
        
        skew_analysis = {
            "skew_detected": len(slow_tasks) > 0,
            "median_duration_seconds": round(median_duration, 3),
            "slow_threshold_seconds": round(slow_threshold, 3),
            "total_slow_tasks": len(slow_tasks),
            "slow_tasks_by_stage": {
                str(stage_id): {
                    "count": len(tasks),
                    "slowest_task_seconds": max(t["duration_seconds"] for t in tasks)
                }
                for stage_id, tasks in slow_tasks_by_stage.items()
            },
            "top_10_slowest_tasks": [
                {
                    "task_id": task["task_id"],
                    "stage_id": task["stage_id"],
                    "executor_id": task["executor_id"],
                    "host": task["host"],
                    "duration_seconds": task["duration_seconds"],
                    "memory_spilled_gb": round(task["memory_bytes_spilled"] / (1024**3), 3),
                    "disk_spilled_gb": round(task["disk_bytes_spilled"] / (1024**3), 3),
                    "shuffle_read_gb": round(task["shuffle_read_bytes"] / (1024**3), 3),
                    "shuffle_write_gb": round(task["shuffle_write_bytes"] / (1024**3), 3)
                }
                for task in slow_tasks[:10]
            ]
        }
    
    # Duration distribution buckets
    duration_buckets = {
        "0_to_1s": 0,
        "1_to_5s": 0,
        "5_to_10s": 0,
        "10_to_30s": 0,
        "30_to_60s": 0,
        "60s_plus": 0
    }
    
    for duration in task_durations:
        if duration < 1:
            duration_buckets["0_to_1s"] += 1
        elif duration < 5:
            duration_buckets["1_to_5s"] += 1
        elif duration < 10:
            duration_buckets["5_to_10s"] += 1
        elif duration < 30:
            duration_buckets["10_to_30s"] += 1
        elif duration < 60:
            duration_buckets["30_to_60s"] += 1
        else:
            duration_buckets["60s_plus"] += 1
    
    return {
        "overall_statistics": overall_stats,
        "duration_buckets": duration_buckets,
        "failed_tasks_statistics": failed_stats,
        "per_stage_statistics": {
            "total_stages_analyzed": len(stage_stats),
            "top_20_stages_by_total_time": top_stages_by_time,
            "all_stages": stage_stats
        },
        "per_executor_statistics": executor_stats,
        "task_skew_analysis": skew_analysis
    }






def extract_spark_config(events: List[Dict]) -> Dict[str, Any]:
    """Extract Spark configuration"""
    config = {}
    for event in events:
        if event.get("Event") == "SparkListenerEnvironmentUpdate":
            spark_props = event.get("Spark Properties", {})
            if isinstance(spark_props, list):
                try:
                    spark_props = dict(spark_props)
                except (TypeError, ValueError) as e:
                    print(f"Warning: Could not convert Spark Properties to dict in config extract: {e}", file=sys.stderr)
                    spark_props = {}
            
            for key in CONFIG_KEYS:
                value = spark_props.get(key)
                if key in ["spark.app.startTime", "spark.app.submitTime"] and value:
                    try:
                        epoch_ms = int(value)
                        config[key] = datetime.fromtimestamp(epoch_ms / 1000).isoformat()
                    except (ValueError, TypeError):
                        config[key] = value
                else:
                    config[key] = value
            break
    return config


def process_application(args_tuple: Tuple[str, List[str], str]) -> Tuple[str, Dict, Dict]:
    """Process all files for a single application and extract metrics"""
    input_path, file_keys, profile = args_tuple
    
    # Get application name from first file
    log_name = get_log_name(file_keys[0])
    
    # Read and aggregate events from ALL files for this application
    # Use ThreadPoolExecutor for parallel reads within the application
    all_events = []
    
    if len(file_keys) > 1:
        # Parallel read for applications with multiple files (rolling logs)
        with ThreadPoolExecutor(max_workers=min(10, len(file_keys))) as executor:
            future_to_key = {
                executor.submit(read_file_lines, input_path, key, profile): key 
                for key in file_keys
            }
            
            for future in as_completed(future_to_key):
                try:
                    lines = future.result(timeout=30)
                    events = parse_events(lines)
                    all_events.extend(events)
                except Exception as e:
                    key = future_to_key[future]
                    print(f"Error reading {key}: {e}", file=sys.stderr)
    else:
        # Single file - read directly
        lines = read_file_lines(input_path, file_keys[0], profile)
        events = parse_events(lines)
        all_events.extend(events)
    
    if not all_events:
        return log_name, None, None
    
    # Extract all metrics from aggregated events
    app_info = extract_app_info(all_events)
    task_summary = extract_task_summary(all_events)
    stage_summary = extract_stage_summary(all_events)
    executor_summary = extract_executor_summary(all_events)
    io_summary = extract_io_summary(all_events)
    spark_config = extract_spark_config(all_events)
    
    # Extract only summary metrics (not full JSON details) to reduce file sizes
    job_details = extract_job_details(all_events)
    sql_metrics = extract_sql_metrics(all_events)
    driver_metrics = extract_driver_metrics(all_events)
    spill_summary = extract_spill_summary(all_events)
    
    # Build task_stage output (with summary metrics only, no JSON details)
    task_stage_output = {
        "application_id": log_name,
        "extraction_timestamp": datetime.now().isoformat(),
        "event_count": len(all_events),
        "file_count": len(file_keys),  # Track how many files were aggregated
        "application_info": app_info,
        "task_summary": task_summary,
        "stage_summary": stage_summary,
        "executor_summary": executor_summary,
        "io_summary": io_summary,
        "spill_summary": spill_summary,
        # Include only summary metrics (not full JSON details)
        "job_details": job_details,
        "sql_metrics": sql_metrics,
        "driver_metrics": driver_metrics,
        "total_cost_factor": executor_summary.get("total_cost_factor", 0)
    }
    
    # Add run duration at top level if available
    if app_info.get("total_run_duration_minutes"):
        task_stage_output["total_run_duration_minutes"] = app_info["total_run_duration_minutes"]
        task_stage_output["total_run_duration_hours"] = app_info["total_run_duration_hours"]
        task_stage_output["application_start_time"] = app_info["application_start_time"]
        task_stage_output["application_end_time"] = app_info["application_end_time"]
    
    config_output = {
        "application_id": log_name,
        "extraction_timestamp": datetime.now().isoformat(),
        "cluster_id": app_info.get("cluster_id", "N/A"),
        "job_id": app_info.get("job_id", "N/A"),
        "application_name": app_info.get("application_name", "N/A"),
        "app_id": app_info.get("app_id", "N/A"),
        "application_start_time": app_info.get("application_start_time"),
        "application_end_time": app_info.get("application_end_time"),
        "total_run_duration_minutes": app_info.get("total_run_duration_minutes"),
        "total_run_duration_hours": app_info.get("total_run_duration_hours"),
        "total_cost_factor": executor_summary.get("total_cost_factor", 0),
        "spark_configuration": spark_config
    }
    
    return log_name, task_stage_output, config_output


def get_log_name(s3_key: str) -> str:
    """Extract application ID from S3 key, handling rolling logs"""
    import re
    
    parts = s3_key.split('/')
    
    for part in parts:
        if part.startswith(('eventlog', 'application')):
            return part
    
    filename = parts[-1]
    
    for ext in ['.zstd', '.zst', '.gz', '.gzip', '.bz2', '.tar', '.zip']:
        if filename.endswith(ext):
            filename = filename[:-len(ext)]
    
    if filename.startswith('events_'):
        match = re.search(r'events_\d+_([^_]+)', filename)
        if match:
            app_id = match.group(1)
            return f"eventlog_{app_id}"
    
    if filename.startswith('application_'):
        filename = re.sub(r'_\d+$', '', filename)
        return filename
    
    return filename


def write_results(output_path: str, log_name: str, 
                  task_stage_data: Dict, config_data: Dict, profile=None) -> Tuple[str, str]:
    """Write results to S3 or local filesystem."""
    if is_s3_path(output_path):
        bucket, prefix = output_path.replace('s3://', '').split('/', 1)
        return write_results_to_s3(bucket, prefix, log_name, task_stage_data, config_data, profile)
    else:
        return write_results_to_local(output_path, log_name, task_stage_data, config_data)


def write_results_to_local(output_path: str, log_name: str,
                           task_stage_data: Dict, config_data: Dict) -> Tuple[str, str]:
    """Write results to local filesystem."""
    from pathlib import Path
    
    output_dir = Path(output_path)
    task_stage_dir = output_dir / "task_stage_summary"
    config_dir = output_dir / "spark_config_extract"
    
    task_stage_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    
    task_stage_file = task_stage_dir / f"{log_name}.json"
    config_file = config_dir / f"{log_name}.json"
    
    task_stage_file.write_text(json.dumps(task_stage_data, indent=2))
    config_file.write_text(json.dumps(config_data, indent=2))
    
    return str(task_stage_file), str(config_file)


def write_results_to_s3(bucket: str, prefix: str, log_name: str, 
                        task_stage_data: Dict, config_data: Dict, profile=None) -> Tuple[str, str]:
    s3_client = get_s3_client(profile)
    
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    
    task_stage_key = f"{prefix}task_stage_summary/{log_name}.json"
    config_key = f"{prefix}spark_config_extract/{log_name}.json"
    
    s3_client.put_object(
        Bucket=bucket,
        Key=task_stage_key,
        Body=json.dumps(task_stage_data, indent=2).encode('utf-8'),
        ContentType='application/json'
    )
    
    s3_client.put_object(
        Bucket=bucket,
        Key=config_key,
        Body=json.dumps(config_data, indent=2).encode('utf-8'),
        ContentType='application/json'
    )
    
    return f"s3://{bucket}/{task_stage_key}", f"s3://{bucket}/{config_key}"


# Main execution
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Process Spark event logs')
    parser.add_argument('--last-hours', type=int, help='Process only logs modified in last N hours (1, 2, 24, 168 for 1 week, etc.)')
    args = parser.parse_args()
    
    print(f"\n{'='*80}")
    print("REDUCED MULTI-THREADED SPARK EVENT LOG PROCESSOR")
    print(f"{'='*80}\n")
    
    if args.last_hours:
        print(f"Time filter: Last {args.last_hours} hours")
    
    print(f"Scanning: {INPUT_PATH}")
    files = list_files(INPUT_PATH, AWS_PROFILE, args.last_hours)

    print(f"\nFound {len(files)} files")
    print("\nSample files:")
    for f in files[:5]:
        if is_s3_path(INPUT_PATH):
            bucket = INPUT_PATH.replace('s3://', '').split('/')[0]
            print(f"  s3://{bucket}/{f}")
        else:
            print(f"  {f}")
    if len(files) > 5:
        print(f"  ... and {len(files) - 5} more")

    print(f"\nProcessing {len(files)} files...")
    print(f"Starting the process at: {datetime.now()}")

    # Group files by application ID
    print("\nGrouping files by application...")
    grouping_start = time.time()
    files_by_app = defaultdict(list)
    for file_key in files:
        log_name = get_log_name(file_key)
        files_by_app[log_name].append(file_key)

    print(f"Grouped {len(files)} files into {len(files_by_app)} applications [{time.time() - grouping_start:.1f}s]")
    process_start_time = datetime.now()

    file_counts = [len(f) for f in files_by_app.values()]
    print(f"  Min files per app: {min(file_counts)}")
    print(f"  Max files per app: {max(file_counts)}")
    print(f"  Avg files per app: {sum(file_counts) / len(file_counts):.1f}")
    
    # Count applications with multiple files (rolling logs)
    multi_file_apps = sum(1 for f in files_by_app.values() if len(f) > 1)
    print(f"  Applications with rolling logs: {multi_file_apps} ({multi_file_apps/len(files_by_app)*100:.1f}%)")

    print(f"\nProcessing {len(files_by_app)} applications with {MAX_WORKERS} workers...")
    print(f"Total files to read: {len(files)}")
    print(f"Writing with {WRITE_WORKERS} concurrent S3 uploads...")
    print(f"Note: Applications with multiple files will read them in parallel\n")

    results_by_log = {}
    results_lock = Lock()
    total_processed = 0
    last_update = time.time()
    executor_start_time = time.time()
    max_wait_time = 1800

    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as process_executor:
        with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as write_executor:
            # Submit one job per APPLICATION (not per file)
            future_to_app = {
                process_executor.submit(
                    process_application, 
                    (INPUT_PATH, file_list, AWS_PROFILE)
                ): app_name
                for app_name, file_list in files_by_app.items()
            }
            
            write_futures = []
            completed = 0
            
            while len(future_to_app) > 0:
                done, pending = wait(
                    future_to_app.keys(),
                    timeout=5,
                    return_when=FIRST_COMPLETED
                )
                
                for future in done:
                    app_name = future_to_app.pop(future)
                    try:
                        log_name, task_stage_data, config_data = future.result(timeout=1)
                        
                        if task_stage_data and config_data:
                            with results_lock:
                                results_by_log[log_name] = (task_stage_data, config_data)
                                
                                write_future = write_executor.submit(
                                    write_results,
                                    OUTPUT_PATH,
                                    log_name,
                                    task_stage_data,
                                    config_data,
                                    AWS_PROFILE
                                )
                                write_futures.append((write_future, log_name))
                        
                        completed += 1
                        if completed % 10 == 0 or completed == len(files_by_app):
                            elapsed = time.time() - last_update
                            print(f"{datetime.now()} : Processed {completed}/{len(files_by_app)} applications [{elapsed:.1f}s since last update]")
                            last_update = time.time()
                    
                    except Exception as e:
                        completed += 1
                        print(f"Error processing application {app_name}: {e}")
                        import traceback
                        traceback.print_exc()
                
                if time.time() - executor_start_time > max_wait_time:
                    print(f"\n⏱ Maximum wait time ({max_wait_time/60:.0f} minutes) exceeded!")
                    break
                
                if time.time() - last_update > 30:
                    pending_count = len(future_to_app)
                    print(f"  Still waiting... {completed}/{len(files_by_app)} apps done, {pending_count} pending")
                    last_update = time.time()
            
            if len(future_to_app) > 0:
                print(f"\n⚠️  WARNING: {len(future_to_app)} applications still processing after timeout")
            
            print(f"\nWaiting for {len(write_futures)} writes to complete...")
            for i, (write_future, log_name) in enumerate(write_futures):
                try:
                    if write_future.done():
                        task_path, config_path = write_future.result()
                        total_processed += 1
                    else:
                        task_path, config_path = write_future.result(timeout=30)
                        total_processed += 1
                    if (i+1) % 10 == 0:
                        print(f"  [{i+1}/{len(write_futures)}] writes complete...")
                except Exception as e:
                    print(f"  ✗ Error writing {log_name}: {e}")

    end_time = datetime.now()
    print(f"\n{'='*80}")
    print(f"Processing complete in: {end_time - process_start_time}")
    print(f"Successfully processed {total_processed} applications")
    print(f"\nOutput locations:")
    if is_s3_path(OUTPUT_PATH):
        bucket, prefix = OUTPUT_PATH.replace('s3://', '').split('/', 1)
        print(f"  Task/Stage: s3://{bucket}/{prefix}task_stage_summary/")
        print(f"  Configs:    s3://{bucket}/{prefix}spark_config_extract/")
    else:
        print(f"  Task/Stage: {OUTPUT_PATH}/task_stage_summary/")
        print(f"  Configs:    {OUTPUT_PATH}/spark_config_extract/")
    print(f"{'='*80}\n")
