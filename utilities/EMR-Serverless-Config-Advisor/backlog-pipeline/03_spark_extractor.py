#!/usr/bin/env python3
"""
Spark-based event log extractor — reads zstd-compressed event logs directly
from S3 using streaming decompression (no local staging required).

Falls back to local-disk decompression (Phase A) when --local-decompress is set.

Usage:
    python3 spark_extractor.py \
        --input s3://bucket/event-logs/ \
        --output s3://bucket/mcp-staging/run_id/ \
        --hours-ago 1
"""

import argparse
import gzip
import bz2
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO

sys.setrecursionlimit(10000)

import boto3
import zstandard as zstd


# ── S3 app discovery (shared by both modes) ─────────────────────────

def discover_apps(input_path, hours_ago=1, max_recent_files=0):
    """Discover application prefixes from S3.
    - Automatically skips .inprogress files (incomplete uploads)
    - If max_recent_files > 0: returns the N most recent files by modification time
    - If max_recent_files = 0: returns files modified within the last N hours (default behavior)
    Returns (bucket, [(s3_prefix, app_name, is_rolling)])."""
    parts = input_path.replace("s3://", "").split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    s3 = boto3.client("s3", region_name="us-east-1")

    # Determine filtering mode
    if max_recent_files > 0:
        print(f"[DEBUG] Mode: Get {max_recent_files} most recent files")
    else:
        print(f"[DEBUG] Mode: Get files from last {hours_ago} hour(s)")

    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours_ago)

    print(f"[DEBUG] Current UTC time: {datetime.now(timezone.utc)}")
    print(f"[DEBUG] Scanning S3: s3://{bucket}/{prefix}")

    # Collect all files
    all_files = []
    file_count = 0
    paginator = s3.get_paginator("list_objects_v2")

    print(f"[DEBUG] Listing all objects (skipping .inprogress files)...")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]

            # Skip .inprogress files (incomplete uploads)
            if key.endswith(".inprogress"):
                print(f"[DEBUG] Skipping .inprogress file: {key}")
                continue

            file_count += 1
            last_modified = obj["LastModified"]

            all_files.append((key, last_modified))

            # Show first few files for debugging
            if file_count <= 5:
                print(f"[DEBUG] File {file_count}: {key}")
                print(f"        LastModified: {last_modified}")

    print(f"[DEBUG] Scanned {file_count} total files")

    # Filter based on mode
    if max_recent_files > 0:
        # Sort by modification time descending and take top N
        all_files.sort(key=lambda x: x[1], reverse=True)
        recent_files = all_files[:max_recent_files]
        print(f"[DEBUG] Selected {len(recent_files)} most recent files")
    else:
        # Time-based filtering
        recent_files = [f for f in all_files if f[1] >= cutoff_time]
        print(f"[DEBUG] Cutoff time: {cutoff_time} (last {hours_ago} hour(s))")
        print(f"[DEBUG] Found {len(recent_files)} files modified in last {hours_ago} hour(s)")

    print(f"[DEBUG] Scanned {file_count} total files, found {len(recent_files)} modified in last {hours_ago} hour(s)")

    if recent_files:
        print(f"[DEBUG] Sample recent files:")
        for key, mod_time in recent_files[:3]:
            print(f"        - {key} (modified: {mod_time})")

    # Now extract unique application prefixes from recent files
    app_prefix_map = {}  # app_prefix -> (app_name, is_rolling, most_recent_time)

    for key, mod_time in recent_files:
        # Pattern 1: eventlog_v2_<app_id>/events_*
        if "/events_" in key and "eventlog_v2_" in key:
            # Extract the eventlog_v2_<app_id> part
            parts = key.split("/")
            for i, part in enumerate(parts):
                if part.startswith("eventlog_v2_"):
                    app_prefix = "/".join(parts[:i+1]) + "/"
                    app_name = part
                    if app_prefix not in app_prefix_map or mod_time > app_prefix_map[app_prefix][2]:
                        app_prefix_map[app_prefix] = (app_name, True, mod_time)
                    break

        # Pattern 2: application_<timestamp>_<app_id> (single file)
        elif "application_" in key:
            filename = key.rsplit("/", 1)[-1]
            if filename.startswith("application_") and not key.endswith("/"):
                # This is a direct file, not a directory
                if key not in app_prefix_map or mod_time > app_prefix_map[key][2]:
                    app_prefix_map[key] = (filename, False, mod_time)

    # Convert to list format
    app_prefixes = [(pfx, name, is_rolling) for pfx, (name, is_rolling, _) in app_prefix_map.items()]

    print(f"[RESULT] Found {len(app_prefixes)} applications with files modified in the last {hours_ago} hour(s)")
    for pfx, name, is_rolling in app_prefixes:
        print(f"  ✓ {name} (rolling={is_rolling}) at {pfx}")

    return bucket, app_prefixes


def list_app_files(bucket, app_prefix, is_rolling):
    """List S3 keys for a single app's event log files (skips .inprogress files)."""
    s3 = boto3.client("s3", region_name="us-east-1")
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=app_prefix):
        for obj in page.get("Contents", []):
            k = obj["Key"]

            # Skip .inprogress files (incomplete uploads)
            if k.endswith(".inprogress"):
                continue

            if is_rolling:
                if "/events_" in k:
                    keys.append(k)
            elif not k.endswith("/"):
                keys.append(k)
    return keys


# ── Streaming S3 decompress (replaces Phase A) ──────────────────────

def read_app_from_s3(spark, bucket, app_prefix, app_name, is_rolling):
    """Read and decompress an app's event logs from S3 directly into a Spark DataFrame.
    Uses binaryFile + streaming zstd decompression — no local disk needed."""

    def clean_duplicate_timestamp(json_line):
        """Remove duplicate timestamp fields to avoid COLUMN_ALREADY_EXISTS error."""
        try:
            import json as json_lib
            obj = json_lib.loads(json_line)
            # If both 'timestamp' and 'Timestamp' exist, keep 'Timestamp' only
            if 'timestamp' in obj and 'Timestamp' in obj:
                del obj['timestamp']
                return json_lib.dumps(obj)
            return json_line
        except:
            return json_line

    s3_path = f"s3://{bucket}/{app_prefix}"
    if not s3_path.endswith("/") and is_rolling:
        s3_path += "/"

    if is_rolling:
        # Directory of zstd files — use binaryFile with streaming decompress
        raw = (spark.read.format("binaryFile").load(s3_path)
               .filter("path LIKE '%.zstd'"))

        def stream_decompress(iterator):
            import zstandard, io
            dctx = zstandard.ZstdDecompressor()
            for row in iterator:
                reader = dctx.stream_reader(io.BytesIO(row.content))
                for line in io.TextIOWrapper(reader, encoding="utf-8"):
                    line = line.strip()
                    if line:
                        yield (clean_duplicate_timestamp(line),)

        lines_rdd = raw.rdd.mapPartitions(stream_decompress)
        lines_df = spark.createDataFrame(lines_rdd, ["line"])
        return spark.read.option("mode", "PERMISSIVE").json(lines_df.select("line").rdd.map(lambda r: r[0]))
    else:
        # Single bare file — read directly from S3
        keys = list_app_files(bucket, app_prefix, False)
        if not keys:
            return None
        s3_paths = [f"s3://{bucket}/{k}" for k in keys]
        # Check if any files are compressed
        compressed = [p for p in s3_paths if any(p.endswith(ext) for ext in (".zstd", ".zst", ".gz", ".gzip", ".bz2"))]
        if not compressed:
            # Plain text — Spark can read directly, but clean duplicates first
            text_rdd = spark.sparkContext.textFile(",".join(s3_paths))
            cleaned_rdd = text_rdd.map(clean_duplicate_timestamp)
            return spark.read.option("mode", "PERMISSIVE").json(cleaned_rdd)
        # Compressed bare files — download on driver, parallelize
        s3_client = boto3.client("s3", region_name="us-east-1")
        all_lines = []
        for key in keys:
            raw = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
            if key.endswith((".zstd", ".zst")):
                dctx = zstd.ZstdDecompressor()
                text = dctx.stream_reader(BytesIO(raw)).read().decode("utf-8", errors="ignore")
            elif key.endswith((".gz", ".gzip")):
                text = gzip.decompress(raw).decode("utf-8", errors="ignore")
            elif key.endswith(".bz2"):
                text = bz2.decompress(raw).decode("utf-8", errors="ignore")
            else:
                text = raw.decode("utf-8", errors="ignore")
            all_lines.extend(line.strip() for line in text.splitlines() if line.strip())
        if not all_lines:
            return None
        # Clean duplicates before parallelizing
        cleaned_lines = [clean_duplicate_timestamp(line) for line in all_lines]

        # Fix for large files: Calculate partitions to ensure each partition is manageable
        # For very large files (>1M lines), use aggressive partitioning to avoid OOM
        sample_size = min(1000, len(cleaned_lines))
        avg_line_size = sum(len(line.encode('utf-8')) for line in cleaned_lines[:sample_size]) / sample_size
        total_size_bytes = avg_line_size * len(cleaned_lines)
        total_size_mb = total_size_bytes / (1024*1024)

        # Use 10MB target per partition to stay well below memory limits
        # For very large datasets (>500MB), use even more aggressive partitioning
        if total_size_mb > 500:
            target_partition_size = 5 * 1024 * 1024  # 5 MB per partition for large datasets
        else:
            target_partition_size = 10 * 1024 * 1024  # 10 MB per partition for smaller datasets

        min_partitions = max(int(total_size_bytes / target_partition_size) + 1, 1)
        # Also ensure at least 1 partition per 5000 lines
        line_based_partitions = len(cleaned_lines) // 5000
        num_partitions = max(min_partitions, line_based_partitions, 1)

        print(f"  Parallelizing {len(cleaned_lines)} lines into {num_partitions} partitions (estimated {total_size_mb:.1f} MB)", flush=True)
        print(f"  Target partition size: ~{total_size_mb/num_partitions:.1f} MB per partition", flush=True)

        # For extremely large datasets, process in batches to avoid driver OOM
        if len(cleaned_lines) > 1000000:  # More than 1M lines
            print(f"  Large dataset detected, using batch processing...", flush=True)
            batch_size = 500000  # Process 500k lines at a time
            dfs = []
            for i in range(0, len(cleaned_lines), batch_size):
                batch = cleaned_lines[i:i+batch_size]
                batch_partitions = max(len(batch) // 5000, 1)
                print(f"  Processing batch {i//batch_size + 1}: {len(batch)} lines in {batch_partitions} partitions", flush=True)
                batch_df = spark.read.option("mode", "PERMISSIVE").json(
                    spark.sparkContext.parallelize(batch, batch_partitions))
                dfs.append(batch_df)
            # Union all batches
            from functools import reduce
            return reduce(lambda df1, df2: df1.union(df2), dfs)
        else:
            return spark.read.option("mode", "PERMISSIVE").json(spark.sparkContext.parallelize(cleaned_lines, num_partitions))


# ── Phase A: Python decompress (legacy fallback) ────────────────────

def _decompress_one_file(args):
    """Download and decompress a single S3 file. Returns (app_name, lines_text)."""
    bucket, key, app_name = args
    s3_client = boto3.client("s3", region_name="us-east-1")
    dctx = zstd.ZstdDecompressor()
    try:
        raw = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
        if key.endswith((".zstd", ".zst")):
            chunks = []
            with dctx.stream_reader(BytesIO(raw)) as reader:
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8", errors="ignore")
        elif key.endswith((".gz", ".gzip")):
            text = gzip.decompress(raw).decode("utf-8", errors="ignore")
        elif key.endswith(".bz2"):
            text = bz2.decompress(raw).decode("utf-8", errors="ignore")
        else:
            text = raw.decode("utf-8", errors="ignore")
        return app_name, text
    except Exception as e:
        print(f"  Warning: {key}: {e}", file=sys.stderr)
        return app_name, ""


def phase_a_decompress(input_path, local_base, hours_ago=1, max_recent_files=0, workers=50):
    """Decompress all apps from S3 to local jsonl files — flat parallelism."""
    bucket, app_prefixes = discover_apps(input_path, hours_ago, max_recent_files)

    all_tasks = []
    for app_prefix, app_name, is_rolling in app_prefixes:
        for key in list_app_files(bucket, app_prefix, is_rolling):
            all_tasks.append((bucket, key, app_name))

    print(f"Phase A: Decompressing {len(app_prefixes)} apps, {len(all_tasks)} files with {workers} threads")
    os.makedirs(local_base, exist_ok=True)

    app_files = {}
    app_counts = {}
    for _, app_name, _ in app_prefixes:
        d = os.path.join(local_base, app_name)
        os.makedirs(d, exist_ok=True)
        app_files[app_name] = open(os.path.join(d, "events.jsonl"), "w")
        app_counts[app_name] = 0

    start = time.time()
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for app_name, text in pool.map(_decompress_one_file, all_tasks):
            if text:
                f = app_files[app_name]
                for line in text.splitlines():
                    line = line.strip()
                    if line:
                        f.write(line + "\n")
                        app_counts[app_name] += 1
            completed += 1
            if completed % 50 == 0 or completed == len(all_tasks):
                print(f"  Decompressing... {completed}/{len(all_tasks)} files", flush=True)

    for f in app_files.values():
        f.close()

    elapsed = time.time() - start
    total_lines = sum(app_counts.values())
    for app_name, count in app_counts.items():
        print(f"  ✓ {app_name}: {count} events")
    print(f"Phase A done: {len(app_prefixes)} apps, {total_lines} events in {elapsed:.1f}s")
    return list(app_counts.keys())


# ── Phase B: Spark extraction ────────────────────────────────────────

def phase_b_spark_extract(app_names, local_base, output_path, hours_ago=1,
                          s3_mode=False, bucket=None, app_prefixes=None, spark=None):
    """Use Spark to extract metrics. Reads from local disk or directly from S3."""
    from pyspark.sql import SparkSession, functions as F

    # Note: spark.rpc.message.maxSize must be set during SparkSession creation
    # It cannot be modified at runtime. If needed, set it in the SparkConf before
    # creating the SparkSession (see orchestrator22.py get_spark_session function)

    GB = 1024 ** 3
    results = []
    total_apps = len(app_names)
    mode_label = "S3 streaming" if s3_mode else "local files"

    # Track processing statistics
    total_records_read = 0
    failed_records = []  # List of (app_id, error_message)
    skipped_records = []  # List of app_ids that were skipped

    # Graceful exit if no apps to process
    if total_apps == 0:
        print(f"\nNo applications to extract. Exiting gracefully.")
        return

    print(f"\nExtracting metrics from {total_apps} apps using Spark ({mode_label})", flush=True)

    # Build lookup for S3 mode
    app_s3_info = {}
    if s3_mode and app_prefixes:
        for pfx, name, is_rolling in app_prefixes:
            app_s3_info[name] = (pfx, is_rolling)

    # Infer schema once from the first app, reuse for all others
    schema = None
    if not s3_mode:
        for app_id in app_names:
            p = os.path.join(local_base, app_id, "events.jsonl")
            if os.path.exists(p) and os.path.getsize(p) > 0:
                schema = spark.read.json("file://" + p).schema
                print(f"Schema inferred ({len(schema.fields)} fields), reusing for all apps")
                break

    for idx, app_id in enumerate(app_names, 1):
        total_records_read += 1
        print(f"  [{idx}/{total_apps}] Extracting {app_id}...", flush=True)
        try:
            if s3_mode:
                pfx, is_rolling = app_s3_info[app_id]
                df = read_app_from_s3(spark, bucket, pfx, app_id, is_rolling)
                if df is None:
                    print(f"  ### Skip {app_id}: no data")
                    skipped_records.append(app_id)
                    continue
            else:
                jsonl_path = os.path.join(local_base, app_id, "events.jsonl")
                if not os.path.exists(jsonl_path) or os.path.getsize(jsonl_path) == 0:
                    print(f"  ### Skip {app_id}: no data")
                    skipped_records.append(app_id)
                    continue
                if schema:
                    df = spark.read.schema(schema).json("file://" + jsonl_path)
                else:
                    df = spark.read.json("file://" + jsonl_path)
            df.cache()
            total_events = df.count()

            # ── App Info + Config in single pass ─────────────────
            meta_rows = (df.filter(
                F.col("Event").isin(
                    "SparkListenerApplicationStart",
                    "SparkListenerApplicationEnd",
                    "SparkListenerEnvironmentUpdate"
                )
            ).collect())

            app_start_row = None
            app_end_row = None
            env_row = None
            for row in meta_rows:
                evt = row["Event"]
                if evt == "SparkListenerApplicationStart":
                    app_start_row = row
                elif evt == "SparkListenerApplicationEnd":
                    app_end_row = row
                elif evt == "SparkListenerEnvironmentUpdate":
                    env_row = row

            app_name = "N/A"
            spark_app_id = "N/A"
            start_ts = None
            end_ts = None

            if app_start_row:
                app_name = getattr(app_start_row, "App Name", None) or "N/A"
                spark_app_id = getattr(app_start_row, "App ID", None) or "N/A"
                start_ts = app_start_row["Timestamp"]

            if app_end_row:
                end_ts = app_end_row["Timestamp"]

            duration_hours = None
            duration_minutes = None
            if start_ts and end_ts:
                duration_ms = end_ts - start_ts
                duration_minutes = round(duration_ms / (1000 * 60), 2)
                duration_hours = round(duration_ms / (1000 * 60 * 60), 2)

            # ── Spark Config ─────────────────────────────────────
            executor_memory_mb = 8 * 1024
            executor_cores_cfg = 4
            job_id = spark_app_id
            cluster_id = "N/A"
            spark_config = {}

            if env_row:
                try:
                    raw_props = getattr(env_row, "Spark Properties", None)
                    if raw_props:
                        if hasattr(raw_props, "asDict"):
                            spark_config = raw_props.asDict()
                        elif isinstance(raw_props, list):
                            spark_config = {r[0]: r[1] for r in raw_props if len(r) >= 2}
                        elif isinstance(raw_props, dict):
                            spark_config = raw_props

                        mem_str = spark_config.get("spark.executor.memory",
                                  spark_config.get("spark.emr.default.executor.memory", ""))
                        if mem_str:
                            if mem_str[-1].lower() == "g":
                                executor_memory_mb = int(mem_str[:-1]) * 1024
                            elif mem_str[-1].lower() == "m":
                                executor_memory_mb = int(mem_str[:-1])

                        cores_str = spark_config.get("spark.executor.cores",
                                    spark_config.get("spark.emr.default.executor.cores", ""))
                        if cores_str:
                            executor_cores_cfg = int(cores_str)

                        job_id = spark_config.get("spark.job_id",
                                                  spark_config.get("dataproc.job.id", spark_app_id))
                        cluster_id = spark_config.get("spark.emr_cluster_id", "N/A")
                except Exception:
                    pass
            # ── Early exit for empty apps (no tasks/executors/jobs) ──
            df_fields = [f.name for f in df.schema.fields]
            if "Task Metrics" not in df_fields and "Executor ID" not in df_fields:
                print(f"  ⚠ {app_id}: No task/executor data — emitting minimal output")
                _start_iso = datetime.fromtimestamp(start_ts / 1000).isoformat() if start_ts else None
                _end_iso = datetime.fromtimestamp(end_ts / 1000).isoformat() if end_ts else None
                _empty = {
                    "application_id": app_id, "extraction_timestamp": datetime.now().isoformat(),
                    "extraction_engine": "spark", "application_info": {"app_id": spark_app_id, "application_name": app_name, "job_id": job_id},
                    "application_start_time": _start_iso, "application_end_time": _end_iso,
                    "total_run_duration_minutes": duration_minutes, "total_run_duration_hours": duration_hours,
                    "task_summary": {"total_tasks": 0, "completed_tasks": 0, "failed_tasks": 0, "killed_tasks": 0},
                    "stage_summary": {"total_stages": 0, "stages": []},
                    "executor_summary": {"total_executors": 0, "active_executors": 0, "avg_memory_utilization_percent": 0, "avg_cpu_utilization_percent": 0, "idle_core_percentage": 0},
                    "io_summary": {"application_level": {"total_input_bytes": 0, "total_input_gb": 0, "total_output_bytes": 0, "total_output_gb": 0, "total_shuffle_read_bytes": 0, "total_shuffle_read_gb": 0, "total_shuffle_write_bytes": 0, "total_shuffle_write_gb": 0, "tasks_analyzed": 0}},
                    "spill_summary": {"total_memory_spilled_bytes": 0, "total_memory_spilled_gb": 0, "total_disk_spilled_bytes": 0, "total_disk_spilled_gb": 0},
                    "shuffle_data_summary": {"total_shuffle_read_gb": 0, "total_shuffle_write_gb": 0},
                    "total_cost_factor": 0, "driver_metrics": None, "job_details": [], "sql_metrics": {},
                    "executor_timeline": [], "sql_executions": [], "spark_config": spark_config,
                }
                _cfg = {"application_id": app_id, "application_name": app_name, "spark_configuration": spark_config,
                        "application_start_time": _start_iso, "application_end_time": _end_iso,
                        "total_run_duration_minutes": duration_minutes, "total_run_duration_hours": duration_hours, "total_cost_factor": 0}
                results.append((app_id, _empty, _cfg))
                df.unpersist()
                continue


            # ── Task end reason breakdown ────────────────────────
            task_ends = df.filter(F.col("Event") == "SparkListenerTaskEnd")
            # Full schema tree (includes nested fields)
            print("\nTaskEnd schema:")
            #task_ends.printSchema()

            # Detect Task End Reason field structure
            ter_fields = []
            if "Task End Reason" in [f.name for f in task_ends.schema.fields]:
                ter_fields = [f.name for f in task_ends.schema["Task End Reason"].dataType.fields]
            has_reason = "Reason" in ter_fields

            task_reason_col = (
                F.col("`Task End Reason`.Reason") if has_reason
                else F.lit("Success")
            )

            print(f"ter_fields: {ter_fields}")
            print(f"task_reason_col: {task_reason_col}")

            task_status = task_ends.select(task_reason_col.alias("reason")).groupBy("reason").count().collect()
            completed_tasks = sum(r["count"] for r in task_status if r["reason"] == "Success")
            failed_tasks = sum(r["count"] for r in task_status if r["reason"] and ("Failed" in r["reason"] or "Exception" in r["reason"]))
            killed_tasks = sum(r["count"] for r in task_status if r["reason"] and "Killed" in r["reason"])

            # Detect Task Metrics field structure
            task_metrics_fields = []
            if "Task Metrics" in [f.name for f in task_ends.schema.fields]:
                task_metrics_fields = [f.name for f in task_ends.schema["Task Metrics"].dataType.fields]

            # Check nested fields within Task Metrics
            input_metrics_fields = []
            output_metrics_fields = []
            shuffle_read_metrics_fields = []
            shuffle_write_metrics_fields = []

            if "Input Metrics" in task_metrics_fields:
                input_metrics_fields = [f.name for f in task_ends.schema["Task Metrics"].dataType["Input Metrics"].dataType.fields]
            if "Output Metrics" in task_metrics_fields:
                output_metrics_fields = [f.name for f in task_ends.schema["Task Metrics"].dataType["Output Metrics"].dataType.fields]
            if "Shuffle Read Metrics" in task_metrics_fields:
                shuffle_read_metrics_fields = [f.name for f in task_ends.schema["Task Metrics"].dataType["Shuffle Read Metrics"].dataType.fields]
            if "Shuffle Write Metrics" in task_metrics_fields:
                shuffle_write_metrics_fields = [f.name for f in task_ends.schema["Task Metrics"].dataType["Shuffle Write Metrics"].dataType.fields]

            # Check Task Info and Task Executor Metrics
            task_info_fields = []
            task_executor_metrics_fields = []
            if "Task Info" in [f.name for f in task_ends.schema.fields]:
                task_info_fields = [f.name for f in task_ends.schema["Task Info"].dataType.fields]
            if "Task Executor Metrics" in [f.name for f in task_ends.schema.fields]:
                task_executor_metrics_fields = [f.name for f in task_ends.schema["Task Executor Metrics"].dataType.fields]

            # Create conditional columns with empty (None) defaults
            executor_run_time_col = F.col("`Task Metrics`.`Executor Run Time`") if "Executor Run Time" in task_metrics_fields else F.lit(None)
            input_bytes_col = F.col("`Task Metrics`.`Input Metrics`.`Bytes Read`") if "Bytes Read" in input_metrics_fields else F.lit(None)
            output_bytes_col = F.col("`Task Metrics`.`Output Metrics`.`Bytes Written`") if "Bytes Written" in output_metrics_fields else F.lit(None)
            shuffle_remote_col = F.col("`Task Metrics`.`Shuffle Read Metrics`.`Remote Bytes Read`") if "Remote Bytes Read" in shuffle_read_metrics_fields else F.lit(None)
            shuffle_local_col = F.col("`Task Metrics`.`Shuffle Read Metrics`.`Local Bytes Read`") if "Local Bytes Read" in shuffle_read_metrics_fields else F.lit(None)
            shuffle_write_col = F.col("`Task Metrics`.`Shuffle Write Metrics`.`Shuffle Bytes Written`") if "Shuffle Bytes Written" in shuffle_write_metrics_fields else F.lit(None)
            mem_spill_col = F.col("`Task Metrics`.`Memory Bytes Spilled`") if "Memory Bytes Spilled" in task_metrics_fields else F.lit(None)
            disk_spill_col = F.col("`Task Metrics`.`Disk Bytes Spilled`") if "Disk Bytes Spilled" in task_metrics_fields else F.lit(None)
            exec_id_col = F.col("`Task Info`.`Executor ID`") if "Executor ID" in task_info_fields else F.lit(None)
            jvm_heap_col = F.col("`Task Executor Metrics`.JVMHeapMemory") if "JVMHeapMemory" in task_executor_metrics_fields else F.lit(None)
            stage_id_col = F.col("`Stage ID`") if "Stage ID" in [f.name for f in task_ends.schema.fields] else F.lit(None)

            # ── IO + Spill aggregation from TaskEnd ──────────────
            io_agg = task_ends.select(
                executor_run_time_col.alias("run_time"),
                input_bytes_col.alias("input_bytes"),
                output_bytes_col.alias("output_bytes"),
                shuffle_remote_col.alias("shuffle_remote"),
                shuffle_local_col.alias("shuffle_local"),
                shuffle_write_col.alias("shuffle_write"),
                mem_spill_col.alias("mem_spill"),
                disk_spill_col.alias("disk_spill"),
                exec_id_col.alias("exec_id"),
                jvm_heap_col.alias("jvm_heap"),
                stage_id_col.alias("stage_id"),
            )
            print(f"ter_fields: {ter_fields}")
            agg_result = io_agg.agg(
                F.count("*").alias("task_count"),
                F.coalesce(F.sum("input_bytes"), F.lit(0)).alias("total_input"),
                F.coalesce(F.sum("output_bytes"), F.lit(0)).alias("total_output"),
                F.coalesce(F.sum("shuffle_remote"), F.lit(0)).alias("total_shuffle_remote"),
                F.coalesce(F.sum("shuffle_local"), F.lit(0)).alias("total_shuffle_local"),
                F.coalesce(F.sum("shuffle_write"), F.lit(0)).alias("total_shuffle_write"),
                F.coalesce(F.sum("mem_spill"), F.lit(0)).alias("total_mem_spill"),
                F.coalesce(F.sum("disk_spill"), F.lit(0)).alias("total_disk_spill"),
                F.coalesce(F.sum("run_time"), F.lit(0)).alias("total_run_time"),
                F.sum(F.when(F.col("input_bytes") > 0, 1).otherwise(0)).alias("tasks_with_input"),
                F.sum(F.when(F.col("output_bytes") > 0, 1).otherwise(0)).alias("tasks_with_output"),
                F.sum(F.when((F.coalesce(F.col("shuffle_remote"), F.lit(0)) + F.coalesce(F.col("shuffle_local"), F.lit(0))) > 0, 1).otherwise(0)).alias("tasks_with_shuffle_read"),
                F.sum(F.when(F.col("shuffle_write") > 0, 1).otherwise(0)).alias("tasks_with_shuffle_write"),
                F.sum(F.when(F.col("mem_spill") > 0, 1).otherwise(0)).alias("tasks_with_mem_spill"),
                F.sum(F.when(F.col("disk_spill") > 0, 1).otherwise(0)).alias("tasks_with_disk_spill"),
            ).first()

            task_count = agg_result["task_count"]
            total_input = agg_result["total_input"]
            total_output = agg_result["total_output"]
            total_shuffle_read = agg_result["total_shuffle_remote"] + agg_result["total_shuffle_local"]
            total_shuffle_write = agg_result["total_shuffle_write"]

            # ── Executor summary ─────────────────────────────────
            exec_agg = (
                io_agg
                .filter((F.col("exec_id").isNotNull()) & (F.col("exec_id") != "driver"))
                .groupBy("exec_id")
                .agg(
                    F.count("*").alias("tasks"),
                    F.coalesce(F.sum("run_time"), F.lit(0)).alias("total_run_time_ms"),
                    F.coalesce(F.max("jvm_heap"), F.lit(0)).alias("peak_jvm_heap"),
                    F.coalesce(F.sum("input_bytes"), F.lit(0)).alias("exec_input"),
                    F.coalesce(F.sum(F.coalesce(F.col("shuffle_remote"), F.lit(0)) + F.coalesce(F.col("shuffle_local"), F.lit(0)))).alias("exec_shuffle_read"),
                    F.coalesce(F.sum("shuffle_write"), F.lit(0)).alias("exec_shuffle_write"),
                    F.countDistinct("stage_id").alias("exec_stages"),
                )
            )

            exec_added = (
                df.filter(F.col("Event") == "SparkListenerExecutorAdded")
                .select(
                    F.col("`Executor ID`").alias("added_id"),
                    F.col("Timestamp").alias("add_ts"),
                    F.col("`Executor Info`.`Total Cores`").alias("cores"),
                    F.col("`Executor Info`.Host").alias("host"),
                )
            )
            exec_removed = (
                df.filter(F.col("Event") == "SparkListenerExecutorRemoved")
                .select(
                    F.col("`Executor ID`").alias("removed_id"),
                    F.col("Timestamp").alias("remove_ts"),
                )
            )
            # Safely extract remove reason (may not exist in all event log versions)
            er_df = df.filter(F.col("Event") == "SparkListenerExecutorRemoved")
            er_fields = [f.name for f in er_df.schema.fields]
            if "Removed Reason" in er_fields:
                exec_removed = exec_removed.join(
                    er_df.select(F.col("`Executor ID`").alias("_rid"), F.col("`Removed Reason`").alias("remove_reason")),
                    exec_removed["removed_id"] == F.col("_rid"), "left"
                ).drop("_rid")
            else:
                exec_removed = exec_removed.withColumn("remove_reason", F.lit(None).cast("string"))

            exec_full = (
                exec_added
                .join(exec_agg, exec_added["added_id"] == exec_agg["exec_id"], "left")
                .join(exec_removed, exec_added["added_id"] == exec_removed["removed_id"], "left")
                .withColumn("cores", F.coalesce(F.col("cores"), F.lit(executor_cores_cfg)))
                .withColumn("tasks", F.coalesce(F.col("tasks"), F.lit(0)))
                .withColumn("total_run_time_ms", F.coalesce(F.col("total_run_time_ms"), F.lit(0)))
                .withColumn("peak_jvm_heap", F.coalesce(F.col("peak_jvm_heap"), F.lit(0)))
                .withColumn("exec_input", F.coalesce(F.col("exec_input"), F.lit(0)))
                .withColumn("exec_shuffle_read", F.coalesce(F.col("exec_shuffle_read"), F.lit(0)))
                .withColumn("exec_shuffle_write", F.coalesce(F.col("exec_shuffle_write"), F.lit(0)))
                .withColumn("exec_stages", F.coalesce(F.col("exec_stages"), F.lit(0)))
                .withColumn("remove_ts", F.coalesce(F.col("remove_ts"), F.lit(end_ts)))
                .withColumn("uptime_ms",
                    F.when(F.col("add_ts").isNotNull() & F.col("remove_ts").isNotNull(),
                           F.col("remove_ts") - F.col("add_ts")).otherwise(F.lit(0)))
                .withColumn("status",
                    F.when(F.col("removed_id").isNotNull(), F.lit("dead")).otherwise(F.lit("active")))
                .withColumn("mem_util",
                    F.when(F.col("peak_jvm_heap") > 0,
                           (F.col("peak_jvm_heap") / (1024.0 * 1024.0)) / executor_memory_mb * 100)
                    .otherwise(F.lit(0)))
                .withColumn("cpu_util",
                    F.when((F.col("uptime_ms") > 0) & (F.col("cores") > 0) & (F.col("total_run_time_ms") > 0),
                           F.least(F.col("total_run_time_ms") / (F.col("uptime_ms") * F.col("cores")) * 100, F.lit(100.0)))
                    .otherwise(F.lit(0)))
                .withColumn("uptime_hours", F.col("uptime_ms") / (1000.0 * 60 * 60))
                .withColumn("exec_cost",
                    F.col("cores") * F.col("uptime_hours") * 0.05
                    + F.lit(executor_memory_mb / 1024.0) * F.col("uptime_hours") * 0.005)
            )

            # Count executors allocated (from ExecutorAdded events)
            executors_allocated = int(exec_added.count())

            active_count = int(exec_full.filter(F.col("status") == "active").count())
            dead_count = int(exec_full.filter(F.col("status") == "dead").count())

            exec_summary = exec_full.agg(
                F.count("*").alias("total_executors"),
                F.coalesce(F.sum("cores"), F.lit(0)).alias("total_cores"),
                F.round(F.coalesce(F.sum("uptime_hours"), F.lit(0)), 2).alias("total_uptime_hours"),
                F.round(F.coalesce(F.max("peak_jvm_heap"), F.lit(0)) / GB, 2).alias("max_peak_gb"),
                F.round(F.coalesce(F.avg("peak_jvm_heap"), F.lit(0)) / GB, 2).alias("avg_peak_gb"),
                F.round(F.coalesce(F.sum("exec_cost"), F.lit(0)), 4).alias("total_cost"),
            ).first()

            # Non-zero averages to match Python extractor
            mem_avg_row = exec_full.filter(F.col("mem_util") > 0).agg(
                F.round(F.avg("mem_util"), 2).alias("avg")).first()
            cpu_avg_row = exec_full.filter(F.col("cpu_util") > 0).agg(
                F.round(F.avg("cpu_util"), 2).alias("avg")).first()

            total_executors = int(exec_summary["total_executors"] or 0)
            total_cores = int(exec_summary["total_cores"] or 0)
            total_uptime = float(exec_summary["total_uptime_hours"] or 0)
            cost_factor = float(exec_summary["total_cost"] or 0)
            avg_mem_util = float(mem_avg_row["avg"] or 0) if mem_avg_row else 0
            avg_cpu_util = float(cpu_avg_row["avg"] or 0) if cpu_avg_row else 0

            total_task_time_hours = float(agg_result["total_run_time"] or 0) / (1000 * 60 * 60)
            total_core_hours = max(float(exec_full.agg(F.coalesce(F.sum(F.col("cores") * F.col("uptime_hours")), F.lit(0))).first()[0]), 1)
            idle_pct = max(0, round((1 - total_task_time_hours / total_core_hours) * 100, 2))

            executor_memory_gb = executor_memory_mb / 1024

            # Extended executor stats (min/max/median) — filter zeros for median
            ext_stats = exec_full.agg(
                F.round(F.min(F.when(F.col("mem_util") > 0, F.col("mem_util"))), 2).alias("min_mem"),
                F.round(F.max("mem_util"), 2).alias("max_mem"),
                F.round(F.percentile_approx(F.when(F.col("mem_util") > 0, F.col("mem_util")), 0.5), 2).alias("median_mem"),
                F.round(F.min(F.when(F.col("cpu_util") > 0, F.col("cpu_util"))), 2).alias("min_cpu"),
                F.round(F.max("cpu_util"), 2).alias("max_cpu"),
                F.round(F.percentile_approx(F.when(F.col("cpu_util") > 0, F.col("cpu_util")), 0.5), 2).alias("median_cpu"),
            ).first()

            # Dead executor reasons
            dead_reasons = {}
            dead_rows = exec_full.filter(F.col("status") == "dead").select("remove_reason").collect()
            for r in dead_rows:
                reason = r["remove_reason"] or "N/A"
                dead_reasons[reason] = dead_reasons.get(reason, 0) + 1

            # Executor details list
            exec_detail_rows = exec_full.orderBy("added_id").collect()
            executor_details = []
            for r in exec_detail_rows:
                executor_details.append({
                    "executor_id": r["added_id"],
                    "host": r["host"] or "N/A",
                    "total_cores": int(r["cores"] or 0),
                    "add_time": datetime.fromtimestamp(r["add_ts"] / 1000).isoformat() if r["add_ts"] else None,
                    "remove_time": datetime.fromtimestamp(r["remove_ts"] / 1000).isoformat() if r["remove_ts"] and r["removed_id"] else None,
                    "remove_reason": r["remove_reason"] if r["removed_id"] else None,
                    "status": r["status"],
                    "uptime_hours": round(float(r["uptime_hours"] or 0), 2),
                    "total_input_bytes": int(r["exec_input"] or 0),
                    "total_shuffle_read": int(r["exec_shuffle_read"] or 0),
                    "total_shuffle_write": int(r["exec_shuffle_write"] or 0),
                    "peak_memory_gb": round(float(r["peak_jvm_heap"] or 0) / GB, 2),
                    "total_tasks": int(r["tasks"] or 0),
                    "total_stages": int(r["exec_stages"] or 0),
                    "total_input_gb": round(float(r["exec_input"] or 0) / GB, 2),
                    "total_shuffle_read_gb": round(float(r["exec_shuffle_read"] or 0) / GB, 2),
                    "total_shuffle_write_gb": round(float(r["exec_shuffle_write"] or 0) / GB, 2),
                    "memory_utilization_percent": round(float(r["mem_util"] or 0), 2),
                    "cpu_utilization_percent": round(float(r["cpu_util"] or 0), 2),
                    "executor_cost_factor": round(float(r["exec_cost"] or 0), 4),
                })

            # ── Driver metrics ───────────────────────────────────
            driver_info = None
            driver_tasks = io_agg.filter(F.col("exec_id") == "driver")
            driver_task_count = driver_tasks.count()
            # Driver memory from TaskExecutorMetrics if available
            driver_mem_row = driver_tasks.agg(
                F.round(F.coalesce(F.max("jvm_heap"), F.lit(0)) / GB, 2).alias("peak_heap"),
                F.round(F.coalesce(F.avg("jvm_heap"), F.lit(0)) / GB, 2).alias("avg_heap"),
            ).first() if driver_task_count > 0 else None

            driver_cores = int(spark_config.get("spark.driver.cores", "0") or 0)
            driver_mem_str = spark_config.get("spark.driver.memory", "0g")
            driver_mem_gb = 0.0
            if driver_mem_str:
                if driver_mem_str[-1].lower() == "g":
                    driver_mem_gb = float(driver_mem_str[:-1])
                elif driver_mem_str[-1].lower() == "m":
                    driver_mem_gb = float(driver_mem_str[:-1]) / 1024

            driver_host = None
            driver_port = None
            if app_start_row:
                try:
                    driver_host = getattr(app_start_row, "Driver Host", None)
                    driver_port = str(getattr(app_start_row, "Driver Port", None) or "")
                except:
                    pass

            # Driver GC metrics from SparkListenerExecutorMetricsUpdate for driver
            driver_gc_time = 0
            driver_gc_count = 0
            driver_peak_offheap = 0
            driver_avg_offheap = 0
            driver_mem_samples = 0

            # Count jobs/stages submitted from driver perspective
            total_jobs_submitted = int(df.filter(F.col("Event") == "SparkListenerJobStart").count())
            total_stages_submitted = int(df.filter(F.col("Event") == "SparkListenerStageSubmitted").count())

            peak_heap = float(driver_mem_row["peak_heap"]) if driver_mem_row else 0
            avg_heap = float(driver_mem_row["avg_heap"]) if driver_mem_row else 0

            driver_metrics = {
                "driver_id": "driver",
                "host": driver_host or "N/A",
                "port": driver_port or "",
                "cores": driver_cores,
                "memory_mb": int(driver_mem_gb * 1024),
                "start_time": datetime.fromtimestamp(start_ts / 1000).isoformat() if start_ts else None,
                "end_time": datetime.fromtimestamp(end_ts / 1000).isoformat() if end_ts else None,
                "uptime_hours": duration_hours or 0,
                "total_tasks_launched": task_count,
                "total_jobs_submitted": total_jobs_submitted,
                "total_stages_submitted": total_stages_submitted,
                "total_result_bytes_received": 0,
                "peak_jvm_heap_memory_gb": peak_heap,
                "peak_jvm_off_heap_memory_gb": driver_peak_offheap,
                "avg_jvm_heap_memory_gb": avg_heap,
                "avg_jvm_off_heap_memory_gb": driver_avg_offheap,
                "gc_time_ms": driver_gc_time,
                "gc_count": driver_gc_count,
                "memory_metrics_samples": driver_mem_samples,
                "total_result_bytes_received_gb": 0.0,
                "configured_memory_gb": driver_mem_gb,
                "memory_utilization_percent": round(peak_heap / driver_mem_gb * 100, 2) if driver_mem_gb > 0 else 0,
                "avg_gc_time_per_task_ms": 0,
            }

            # ── Job details ──────────────────────────────────────
            job_starts = df.filter(F.col("Event") == "SparkListenerJobStart")
            job_ends = df.filter(F.col("Event") == "SparkListenerJobEnd")

            # Check if Job ID, Submission Time, and Stage IDs columns exist in JobStart events
            js_fields = [f.name for f in job_starts.schema.fields]
            has_job_id_in_starts = "Job ID" in js_fields
            has_submission_time = "Submission Time" in js_fields
            has_stage_ids = "Stage IDs" in js_fields

            # Get submission time from spark config as fallback
            submit_time_from_config = int(spark_config.get("spark.app.submitTime", start_ts or 0))

            # Create conditional columns
            job_id_col = F.col("`Job ID`") if has_job_id_in_starts else F.lit(job_id)
            submission_time_col = F.col("`Submission Time`") if has_submission_time else F.lit(submit_time_from_config)
            stage_ids_col = F.col("`Stage IDs`") if has_stage_ids else F.array(F.lit(1))

            job_start_rows = job_starts.select(
                job_id_col.alias("job_id"),
                submission_time_col.alias("submit_ts"),
                stage_ids_col.alias("stage_ids"),
            ).collect()

            # Safely extract job group from Properties
            job_group_map = {}
            if "Properties" in js_fields:
                try:
                    jg_rows = job_starts.select(
                        job_id_col.alias("jid"),
                        F.col("Properties.`spark.jobGroup.id`").alias("jg"),
                    ).collect()
                    for r in jg_rows:
                        if r["jg"]:
                            job_group_map[r["jid"]] = r["jg"]
                except:
                    pass

            # Check if Completion Time exists in JobEnd events
            je_fields = [f.name for f in job_ends.schema.fields]
            has_completion_time = "Completion Time" in je_fields
            completion_time_col = F.col("`Completion Time`") if has_completion_time else F.lit(end_ts)

            job_end_rows = job_ends.select(
                job_id_col.alias("job_id"),
                completion_time_col.alias("complete_ts"),
            ).collect()
            # Safely get Job Result
            job_result_map = {}
            if "Job Result" in je_fields:
                for r in job_ends.select(job_id_col.alias("jid"), F.col("`Job Result`.Result").alias("result")).collect():
                    job_result_map[r["jid"]] = r["result"]
            job_end_map = {r["job_id"]: r for r in job_end_rows}

            jobs = []
            successful = failed_jobs = running = 0
            for r in job_start_rows:
                jid = r["job_id"]
                end_r = job_end_map.get(jid)
                status = "RUNNING"
                comp_ts = None
                dur_ms = None
                failure = None
                if end_r:
                    result = job_result_map.get(jid, "JobSucceeded")
                    comp_ts = end_r["complete_ts"]
                    if result == "JobSucceeded":
                        status = "SUCCEEDED"
                        successful += 1
                    else:
                        status = "FAILED"
                        failed_jobs += 1
                    if comp_ts and r["submit_ts"]:
                        dur_ms = comp_ts - r["submit_ts"]
                else:
                    running += 1

                sids = list(r["stage_ids"]) if r["stage_ids"] else []
                jobs.append({
                    "job_id": jid,
                    "submission_time": datetime.fromtimestamp(r["submit_ts"] / 1000).isoformat() if r["submit_ts"] else None,
                    "stage_ids": sids,
                    "status": status,
                    "completion_time": datetime.fromtimestamp(comp_ts / 1000).isoformat() if comp_ts else None,
                    "duration_ms": dur_ms,
                    "failure_reason": failure,
                    "job_group": job_group_map.get(jid),
                    "num_stages": len(sids),
                })

            total_jobs = len(jobs)
            durations = [j["duration_ms"] for j in jobs if j["duration_ms"] is not None]
            avg_dur_sec = round(sum(durations) / len(durations) / 1000, 2) if durations else None
            job_details = {
                "summary": {
                    "total_jobs": total_jobs,
                    "successful_jobs": successful,
                    "failed_jobs": failed_jobs,
                    "running_jobs": running,
                    "success_rate_percent": round(successful / total_jobs * 100, 2) if total_jobs > 0 else 0,
                    "avg_duration_seconds": avg_dur_sec,
                },
                "jobs": sorted(jobs, key=lambda j: j["job_id"], reverse=True),
            }

            # ── Per-stage details ─────────────────────────────────
            stage_submitted = df.filter(F.col("Event") == "SparkListenerStageSubmitted")
            stage_completed = df.filter(F.col("Event") == "SparkListenerStageCompleted")

            # Check if Stage Info exists and detect its fields
            stage_info_fields = []
            if "Stage Info" in [f.name for f in stage_completed.schema.fields]:
                stage_info_fields = [f.name for f in stage_completed.schema["Stage Info"].dataType.fields]

            # Create conditional columns for Stage Info fields
            has_stage_id = "Stage ID" in stage_info_fields
            has_stage_name = "Stage Name" in stage_info_fields
            has_num_tasks = "Number of Tasks" in stage_info_fields
            has_submission_time_stage = "Submission Time" in stage_info_fields
            has_completion_time_stage = "Completion Time" in stage_info_fields
            has_failure_reason = "Failure Reason" in stage_info_fields

            stage_id_stage_col = F.col("`Stage Info`.`Stage ID`") if has_stage_id else F.lit(0)
            stage_name_col = F.col("`Stage Info`.`Stage Name`") if has_stage_name else F.lit("unknown")
            num_tasks_col = F.col("`Stage Info`.`Number of Tasks`") if has_num_tasks else F.lit(0)
            submission_time_stage_col = F.col("`Stage Info`.`Submission Time`") if has_submission_time_stage else F.lit(0)
            completion_time_stage_col = F.col("`Stage Info`.`Completion Time`") if has_completion_time_stage else F.lit(0)
            failure_reason_col = F.col("`Stage Info`.`Failure Reason`") if has_failure_reason else F.lit(None).cast("string")

            stage_cols = [
                stage_id_stage_col.alias("stage_id"),
                stage_name_col.alias("stage_name"),
                num_tasks_col.alias("num_tasks"),
                submission_time_stage_col.alias("submit_ts"),
                completion_time_stage_col.alias("complete_ts"),
                failure_reason_col.alias("failure_reason"),
            ]

            stage_times = (
                stage_completed.select(*stage_cols)
                .withColumn("duration_ms", F.col("complete_ts") - F.col("submit_ts"))
            )

            # Per-stage IO from TaskEnd
            stage_io = (
                task_ends.select(
                    stage_id_col.alias("stage_id"),
                    input_bytes_col.alias("input_bytes"),
                    output_bytes_col.alias("output_bytes"),
                    shuffle_remote_col.alias("shuffle_remote"),
                    shuffle_local_col.alias("shuffle_local"),
                    shuffle_write_col.alias("shuffle_write"),
                    mem_spill_col.alias("mem_spill"),
                    disk_spill_col.alias("disk_spill"),
                    executor_run_time_col.alias("run_time"),
                )
                .groupBy("stage_id")
                .agg(
                    F.count("*").alias("tasks_completed"),
                    F.round(F.coalesce(F.sum("input_bytes"), F.lit(0)) / GB, 2).alias("input_gb"),
                    F.round(F.coalesce(F.sum("output_bytes"), F.lit(0)) / GB, 2).alias("output_gb"),
                    F.round((F.coalesce(F.sum("shuffle_remote"), F.lit(0)) + F.coalesce(F.sum("shuffle_local"), F.lit(0))) / GB, 2).alias("shuffle_read_gb"),
                    F.round(F.coalesce(F.sum("shuffle_write"), F.lit(0)) / GB, 2).alias("shuffle_write_gb"),
                    F.round(F.coalesce(F.sum("mem_spill"), F.lit(0)) / GB, 2).alias("mem_spill_gb"),
                    F.round(F.coalesce(F.sum("disk_spill"), F.lit(0)) / GB, 2).alias("disk_spill_gb"),
                    F.round(F.coalesce(F.sum("run_time"), F.lit(0)) / 1000.0, 1).alias("total_task_time_s"),
                )
            )

            stage_details_df = stage_times.join(stage_io, "stage_id", "left")
            stage_details_rows = stage_details_df.orderBy("stage_id").collect()
            stage_details = []
            completed_stages = failed_stage_count = skipped_stages = 0
            for r in stage_details_rows:
                has_failure = r["failure_reason"] is not None
                if has_failure:
                    failed_stage_count += 1
                elif int(r["tasks_completed"] or 0) > 0:
                    completed_stages += 1
                else:
                    skipped_stages += 1
                stage_details.append({
                    "stage_id": int(r["stage_id"]),
                    "name": r["stage_name"][:100] if r["stage_name"] else "",
                    "num_tasks": int(r["num_tasks"] or 0),
                    "tasks_completed": int(r["tasks_completed"] or 0),
                    "duration_sec": round(float(r["duration_ms"] or 0) / 1000, 1),
                    "input_gb": float(r["input_gb"] or 0),
                    "output_gb": float(r["output_gb"] or 0),
                    "shuffle_read_gb": float(r["shuffle_read_gb"] or 0),
                    "shuffle_write_gb": float(r["shuffle_write_gb"] or 0),
                    "mem_spill_gb": float(r["mem_spill_gb"] or 0),
                    "disk_spill_gb": float(r["disk_spill_gb"] or 0),
                    "total_task_time_sec": float(r["total_task_time_s"] or 0),
                    "failure_reason": r["failure_reason"],
                })

            # ── Executor timeline ────────────────────────────────
            added_rows = exec_added.orderBy("add_ts").collect()
            removed_rows = exec_removed.orderBy("remove_ts").collect()
            executor_timeline = (
                [{"time_ms": int(r["add_ts"]), "event": "added", "executor_id": r["added_id"],
                  "cores": int(r["cores"] or executor_cores_cfg)} for r in added_rows]
                + [{"time_ms": int(r["remove_ts"]), "event": "removed", "executor_id": r["removed_id"]}
                   for r in removed_rows]
            )
            executor_timeline.sort(key=lambda x: x["time_ms"])

            # ── SQL plans ────────────────────────────────────────
            sql_starts = df.filter(F.col("Event") == "org.apache.spark.sql.execution.ui.SparkListenerSQLExecutionStart")
            sql_ends = df.filter(F.col("Event") == "org.apache.spark.sql.execution.ui.SparkListenerSQLExecutionEnd")

            # Check if executionId column exists in SQL events
            sql_start_fields = [f.name for f in sql_starts.schema.fields]
            has_execution_id = "executionId" in sql_start_fields
            has_description = "description" in sql_start_fields
            has_physical_plan = "physicalPlanDescription" in sql_start_fields
            has_time_start = "time" in sql_start_fields
            has_details = "details" in sql_start_fields

            # Get executor.id from spark config as fallback for executionId
            executor_id_from_config = spark_config.get("spark.executor.id", "0")

            # Create conditional column for executionId - use spark.executor.id if not found
            execution_id_col = F.col("executionId") if has_execution_id else F.lit(executor_id_from_config)
            description_col = F.col("description") if has_description else F.lit("unknown")
            plan_col = F.col("physicalPlanDescription") if has_physical_plan else F.lit("")
            time_start_col = F.col("time") if has_time_start else F.lit(0)

            sql_start_rows = sql_starts.select(
                execution_id_col.alias("exec_id"),
                description_col.alias("description"),
                plan_col.alias("plan"),
                time_start_col.alias("start_time"),
            ).collect()

            # Also try to get details (stack trace) from SQL start events
            sql_details_map = {}
            if has_details and has_execution_id:
                for r in sql_starts.select(execution_id_col.alias("eid"), F.col("details")).collect():
                    sql_details_map[r["eid"]] = r["details"] or ""

            # Check SQL end fields
            sql_end_fields = [f.name for f in sql_ends.schema.fields]
            has_execution_id_end = "executionId" in sql_end_fields
            has_time_end = "time" in sql_end_fields

            execution_id_end_col = F.col("executionId") if has_execution_id_end else F.lit(executor_id_from_config)
            time_end_col = F.col("time") if has_time_end else F.lit(0)

            sql_end_map = {}
            for r in sql_ends.select(execution_id_end_col.alias("exec_id"), time_end_col.alias("end_time")).collect():
                sql_end_map[r["exec_id"]] = r["end_time"]

            sql_executions = []
            completed_sql = running_sql = 0
            for r in sql_start_rows:
                eid = r["exec_id"]
                end_t = sql_end_map.get(eid)
                dur = (end_t - r["start_time"]) if end_t and r["start_time"] else None
                if end_t:
                    completed_sql += 1
                    status = "COMPLETED"
                else:
                    running_sql += 1
                    status = "RUNNING"
                sql_executions.append({
                    "execution_id": int(eid),
                    "description": (r["description"] or "")[:200],
                    "details": sql_details_map.get(eid, ""),
                    "submission_time": datetime.fromtimestamp(r["start_time"] / 1000).isoformat() if r["start_time"] else None,
                    "completion_time": datetime.fromtimestamp(end_t / 1000).isoformat() if end_t else None,
                    "duration_ms": int(dur) if dur else None,
                    "duration_sec": round(dur / 1000, 1) if dur else None,
                    "status": status,
                    "physical_plan": r["plan"] or "",
                    "physical_plan_description": r["plan"] or "",
                })

            sql_metrics = {
                "total_sql_executions": len(sql_executions),
                "completed_executions": completed_sql,
                "running_executions": running_sql,
                "sql_executions": sql_executions,
            }

            # ── Build output ─────────────────────────────────────
            app_start_iso = datetime.fromtimestamp(start_ts / 1000).isoformat() if start_ts else None
            app_end_iso = datetime.fromtimestamp(end_ts / 1000).isoformat() if end_ts else None

            task_stage_output = {
                "application_id": app_id,
                "extraction_timestamp": datetime.now().isoformat(),
                "extraction_engine": "pyspark",
                "event_count": total_events,
                "file_count": len(list_app_files(bucket, app_s3_info[app_id][0], app_s3_info[app_id][1])) if s3_mode and app_id in app_s3_info else 0,
                "src_event_log_location": f"s3://{bucket}/{app_s3_info[app_id][0]}" if s3_mode and app_id in app_s3_info else "",
                "application_info": {
                    "job_id": job_id, "cluster_id": cluster_id,
                    "application_name": app_name, "app_id": spark_app_id,
                    "application_start_time": app_start_iso,
                    "application_end_time": app_end_iso,
                    "total_run_duration_minutes": duration_minutes,
                    "total_run_duration_hours": duration_hours,
                },
                "application_start_time": app_start_iso,
                "application_end_time": app_end_iso,
                "total_run_duration_minutes": duration_minutes,
                "total_run_duration_hours": duration_hours,
                "task_summary": {
                    "total_tasks": task_count,
                    "completed_tasks": completed_tasks,
                    "failed_tasks": failed_tasks,
                    "killed_tasks": killed_tasks,
                    "success_rate_percent": round(completed_tasks / task_count * 100, 2) if task_count > 0 else 0,
                },
                "stage_summary": {
                    "total_stages": len(stage_details),
                    "completed_stages": completed_stages,
                    "failed_stages": failed_stage_count,
                    "skipped_stages": skipped_stages,
                    "success_rate_percent": round(completed_stages / len(stage_details) * 100, 2) if stage_details else 0,
                    "stages": stage_details,
                },
                "executor_summary": {
                    "total_executors": total_executors,
                    "active_executors": active_count,
                    "dead_executors": dead_count,
                    "dead_executor_reasons": dead_reasons,
                    "total_cores": total_cores,
                    "total_uptime_hours": total_uptime,
                    "total_available_core_hours": round(total_core_hours, 2),
                    "total_task_execution_hours": round(total_task_time_hours, 2),
                    "max_peak_memory_gb": float(exec_summary["max_peak_gb"] or 0),
                    "avg_peak_memory_gb": float(exec_summary["avg_peak_gb"] or 0),
                    "avg_memory_utilization_percent": avg_mem_util,
                    "min_memory_utilization_percent": float(ext_stats["min_mem"] or 0),
                    "max_memory_utilization_percent": float(ext_stats["max_mem"] or 0),
                    "median_memory_utilization_percent": float(ext_stats["median_mem"] or 0),
                    "avg_cpu_utilization_percent": avg_cpu_util,
                    "min_cpu_utilization_percent": float(ext_stats["min_cpu"] or 0),
                    "max_cpu_utilization_percent": float(ext_stats["max_cpu"] or 0),
                    "median_cpu_utilization_percent": float(ext_stats["median_cpu"] or 0),
                    "idle_core_percentage": idle_pct,
                    "total_cost_factor": cost_factor,
                    "cost_calculation_params": {
                        "executor_memory_gb": executor_memory_gb,
                        "executor_cores": executor_cores_cfg,
                        "cost_per_core_hour": 0.05,
                        "cost_per_gb_hour": 0.005,
                    },
                    "memory_calculation_method": {
                        "approach": "jvm_heap_memory",
                        "formula": "(peak_jvm_heap_mb / executor_memory_mb) * 100",
                        "data_source": "TaskExecutorMetrics.JVMHeapMemory",
                        "executor_memory_mb": executor_memory_mb,
                        "note": "Uses actual JVM heap usage from Task Executor Metrics in TaskEnd events",
                    },
                    "driver_info": driver_info,
                    "executor_details": executor_details,
                },
                "io_summary": {
                    "application_level": {
                        "total_input_bytes": int(total_input), "total_input_gb": round(total_input / GB, 2),
                        "total_output_bytes": int(total_output), "total_output_gb": round(total_output / GB, 2),
                        "total_shuffle_read_bytes": int(total_shuffle_read), "total_shuffle_read_gb": round(total_shuffle_read / GB, 2),
                        "total_shuffle_write_bytes": int(total_shuffle_write), "total_shuffle_write_gb": round(total_shuffle_write / GB, 2),
                        "tasks_analyzed": task_count,
                        "tasks_with_input": int(agg_result["tasks_with_input"] or 0),
                        "tasks_with_output": int(agg_result["tasks_with_output"] or 0),
                        "tasks_with_shuffle_read": int(agg_result["tasks_with_shuffle_read"] or 0),
                        "tasks_with_shuffle_write": int(agg_result["tasks_with_shuffle_write"] or 0),
                    }
                },
                "spill_summary": {
                    "total_memory_spilled_bytes": int(agg_result["total_mem_spill"]),
                    "total_memory_spilled_gb": round(agg_result["total_mem_spill"] / GB, 2),
                    "total_disk_spilled_bytes": int(agg_result["total_disk_spill"]),
                    "total_disk_spilled_gb": round(agg_result["total_disk_spill"] / GB, 2),
                    "tasks_with_memory_spill": int(agg_result["tasks_with_mem_spill"] or 0),
                    "tasks_with_disk_spill": int(agg_result["tasks_with_disk_spill"] or 0),
                    "tasks_with_memory_spill_percent": round(int(agg_result["tasks_with_mem_spill"] or 0) / task_count * 100, 2) if task_count > 0 else 0,
                    "tasks_with_disk_spill_percent": round(int(agg_result["tasks_with_disk_spill"] or 0) / task_count * 100, 2) if task_count > 0 else 0,
                    "tasks_analyzed": task_count,
                },
                "shuffle_data_summary": (lambda stages: {
                    "total_shuffle_read_gb": round(total_shuffle_read / GB, 2),
                    "total_shuffle_write_gb": round(total_shuffle_write / GB, 2),
                    "total_disk_spill_gb": round(agg_result["total_disk_spill"] / GB, 2),
                    "max_stage_shuffle_read_gb": round(max((s.get("shuffle_read_gb") or 0 for s in stages), default=0), 2),
                    "max_stage_shuffle_write_gb": round(max((s.get("shuffle_write_gb") or 0 for s in stages), default=0), 2),
                    "max_stage_disk_spill_gb": round(max((s.get("disk_spill_gb") or 0 for s in stages), default=0), 2),
                    "emr_serverless_storage_eligible": max((s.get("shuffle_write_gb") or 0 for s in stages), default=0) <= 200,
                    "emr_serverless_storage_limit_gb": 200,
                })(stage_details),
                "total_cost_factor": cost_factor,
                "driver_metrics": driver_metrics,
                "job_details": job_details,
                "sql_metrics": sql_metrics,
                "executor_timeline": executor_timeline,
                "sql_executions": sql_executions,
                "spark_config": spark_config,            }

            config_output = {
                "application_id": app_id,
                "extraction_timestamp": datetime.now().isoformat(),
                "cluster_id": cluster_id, "job_id": job_id,
                "application_name": app_name, "app_id": spark_app_id,
                "application_start_time": app_start_iso,
                "application_end_time": app_end_iso,
                "total_run_duration_minutes": duration_minutes,
                "total_run_duration_hours": duration_hours,
                "total_cost_factor": cost_factor,
                "spark_configuration": spark_config,
            }

            results.append((app_id, task_stage_output, config_output))
            df.unpersist()
            print(f" ####  ✓ {app_id}: {task_count} tasks, {round(total_input/GB,1)} GB input, {total_executors} executors")

        except Exception as e:
            failed_records.append((app_id, str(e)))
            print(f" ### exception  ✗ {app_id}: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)

    # ── Write results ────────────────────────────────────────────────
    print(f"\nWriting {len(results)} results to {output_path}")

    if output_path.startswith("s3://"):
        parts = output_path.replace("s3://", "").split("/", 1)
        bucket = parts[0]
        prefix = parts[1].rstrip("/") + "/" if len(parts) > 1 else ""
        s3 = boto3.client("s3", region_name="us-east-1")
        for aid, ts, cfg in results:
            s3.put_object(Bucket=bucket, Key=f"{prefix}task_stage_summary/{aid}.json",
                          Body=json.dumps(ts, indent=2), ContentType="application/json")
            s3.put_object(Bucket=bucket, Key=f"{prefix}spark_config_extract/{aid}.json",
                          Body=json.dumps(cfg, indent=2), ContentType="application/json")
    else:
        os.makedirs(f"{output_path}/task_stage_summary", exist_ok=True)
        os.makedirs(f"{output_path}/spark_config_extract", exist_ok=True)
        for aid, ts, cfg in results:
            with open(f"{output_path}/task_stage_summary/{aid}.json", "w") as f:
                json.dump(ts, f, indent=2)
            with open(f"{output_path}/spark_config_extract/{aid}.json", "w") as f:
                json.dump(cfg, f, indent=2)

    # ── Processing Summary ───────────────────────────────────────────
    records_written = len(results)
    print(f"\n{'='*60}")
    print(f"EXTRACTION SUMMARY")
    print(f"{'='*60}")
    print(f"Total records read:          {total_records_read}")
    print(f"Skipped records (no data):   {len(skipped_records)}")
    if skipped_records:
        print(f"  Skipped apps:")
        for app_id in skipped_records:
            print(f"    - {app_id}")
    print(f"Failed records (exceptions): {len(failed_records)}")
    if failed_records:
        print(f"  Failed apps:")
        for app_id, error in failed_records:
            print(f"    - {app_id}: {error[:100]}")  # Truncate long error messages
    print(f"Successfully written:        {records_written}")
    print(f"{'='*60}")

    print(f"✅ Extraction complete: {len(results)} applications")



# ── Job ID Extraction ────────────────────────────────────────────────

def extract_job_id_for_application(output_path, application_id):
    """
    Extract job_id for a SPECIFIC application_id from spark_config_extract.
    Used in single_app mode to avoid reading all files in directory.

    Args:
        output_path: S3 or local path (e.g., s3://bucket/test-target-metrics/20260422T144845Z/)
        application_id: The specific application_id to read (e.g., application_1776841689876_1948)

    Returns:
        List with single job_id if found, empty list otherwise
    """
    job_id = None
    print(f"  DEBUG: Extracting job_id for specific application: {application_id}")

    try:
        if output_path.startswith("s3://"):
            # S3 mode - read specific file
            import boto3
            parts = output_path.replace("s3://", "").split("/", 1)
            bucket = parts[0]
            prefix = parts[1].rstrip("/") + "/" if len(parts) > 1 else ""
            s3_key = f"{prefix}spark_config_extract/{application_id}.json"

            print(f"  DEBUG: Reading s3://{bucket}/{s3_key}")

            s3 = boto3.client("s3", region_name="us-east-1")
            try:
                body = s3.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
                data = json.loads(body)
                job_id = data.get("job_id")
                if job_id:
                    print(f"  ✓ Extracted job_id: {job_id} from {application_id}.json")
                else:
                    print(f"  ⚠ No job_id in {application_id}.json")
            except s3.exceptions.NoSuchKey:
                print(f"  ⚠ File not found: s3://{bucket}/{s3_key}")
            except Exception as e:
                print(f"  ✗ Error reading {s3_key}: {e}")
        else:
            # Local mode - read specific file
            config_file = f"{output_path}/spark_config_extract/{application_id}.json"
            print(f"  DEBUG: Reading {config_file}")

            if os.path.exists(config_file):
                with open(config_file, 'r') as f:
                    data = json.load(f)
                    job_id = data.get("job_id")
                    if job_id:
                        print(f"  ✓ Extracted job_id: {job_id} from {application_id}.json")
                    else:
                        print(f"  ⚠ No job_id in {application_id}.json")
            else:
                print(f"  ⚠ File not found: {config_file}")

    except Exception as e:
        print(f"  ✗ ERROR in extract_job_id_for_application: {e}")
        import traceback
        traceback.print_exc()
        return []

    result = [job_id] if job_id else []
    print(f"  DEBUG: Returning job_ids: {result}")
    return result


def extract_job_ids_from_output_files(output_path):
    """
    Extract job_ids from the JSON files written to output_path.
    Reads spark_config_extract/*.json files (not task_stage_summary) and extracts job_id from each.
    Returns list of job_ids in the order they were processed.
    """
    job_ids = []
    print(f"  DEBUG: Extracting job_ids from spark_config_extract/ in: {output_path}")

    try:
        if output_path.startswith("s3://"):
            # S3 mode - list spark_config_extract files and extract job_ids
            import boto3
            parts = output_path.replace("s3://", "").split("/", 1)
            bucket = parts[0]
            prefix = parts[1].rstrip("/") + "/" if len(parts) > 1 else ""
            s3_prefix = f"{prefix}spark_config_extract/"
            print(f"  DEBUG: S3 mode - bucket: {bucket}, prefix: {s3_prefix}")

            s3 = boto3.client("s3", region_name="us-east-1")

            paginator = s3.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=bucket, Prefix=s3_prefix)

            file_count = 0
            for page in pages:
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith(".json"):
                        file_count += 1
                        try:
                            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                            data = json.loads(body)
                            job_id = data.get("job_id")
                            if job_id:
                                job_ids.append(job_id)
                                print(f"    ✓ Extracted job_id: {job_id}")
                            else:
                                print(f"    ⚠ No job_id in {obj['Key']}")
                        except Exception as e:
                            print(f"    ✗ Error extracting job_id from {obj['Key']}: {e}")
            print(f"  DEBUG: Found {file_count} JSON files in spark_config_extract/, extracted {len(job_ids)} job_ids")
        else:
            # Local mode - read files from filesystem
            config_dir = f"{output_path}/spark_config_extract"
            print(f"  DEBUG: Local mode - directory: {config_dir}")

            if os.path.isdir(config_dir):
                files = sorted(os.listdir(config_dir))
                print(f"  DEBUG: Found {len(files)} files in {config_dir}")

                for filename in files:
                    if filename.endswith(".json"):
                        try:
                            filepath = os.path.join(config_dir, filename)
                            with open(filepath, 'r') as f:
                                data = json.load(f)
                                job_id = data.get("job_id")
                                if job_id:
                                    job_ids.append(job_id)
                                    print(f"    ✓ Extracted job_id: {job_id}")
                                else:
                                    print(f"    ⚠ No job_id in {filename}")
                        except Exception as e:
                            print(f"    ✗ Error extracting job_id from {filename}: {e}")
            else:
                print(f"  DEBUG: Directory does not exist: {config_dir}")

    except Exception as e:
        print(f"  ✗ ERROR in extract_job_ids_from_output_files: {e}")
        import traceback
        traceback.print_exc()
        return None

    print(f"  DEBUG: Final job_ids list: {job_ids}")
    return job_ids  # Return empty list instead of None - important for filtering!


# ── Main ─────────────────────────────────────────────────────────────

def run_extractor(input_path, output_path, hours_ago=1, max_recent_files=0, decompress_workers=50,
                  local_decompress=False, single_app=False, spark=None, application_id=None):
    """Run extractor flow used by both CLI and orchestrators.

    Parameters:
    - hours_ago: For time-based filtering (default: 1 hour). Ignored if max_recent_files > 0
    - max_recent_files: If > 0, get the N most recent files. If 0, use time-based filtering (default)
    - application_id: In single_app mode, the application_id for extracting job_id from specific file
    """
    local_staging = "/tmp/spark_extractor_staging"

    print(f"\n{'='*60}")
    start = time.time()

    if single_app:
        # Single-app mode: input_path points to one app's S3 prefix.
        print("SPARK EXTRACTOR — single app")
        print(f"{'='*60}")
        parts = input_path.replace("s3://", "").rstrip("/").split("/", 1)
        bucket = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        name = prefix.rsplit("/", 1)[-1]
        is_rolling = name.startswith("eventlog_v2_")
        if is_rolling and not prefix.endswith("/"):
            prefix += "/"
        app_prefixes = [(prefix, name, is_rolling)]
        phase_b_spark_extract([name], None, output_path, hours_ago=1,
                              s3_mode=True, bucket=bucket, app_prefixes=app_prefixes,
                              spark=spark)

        # In single_app mode, read job_id from the SPECIFIC file just written
        # This prevents reading all files in the directory (which causes duplicate job_id issue)
        if application_id:
            job_ids = extract_job_id_for_application(output_path, application_id)
        else:
            # Fallback to reading all files (old behavior, not recommended)
            job_ids = extract_job_ids_from_output_files(output_path)
    elif local_decompress:
        print("SPARK EXTRACTOR — local decompress + Spark extract")
        print(f"{'='*60}")
        app_names = phase_a_decompress(input_path, local_staging, hours_ago, max_recent_files, decompress_workers)
        phase_b_spark_extract(app_names, local_staging, output_path, hours_ago, spark=spark)
        # Extract job_ids from all files written
        job_ids = extract_job_ids_from_output_files(output_path)
    else:
        print("SPARK EXTRACTOR — S3 streaming decompress")
        print(f"{'='*60}")
        bucket, app_prefixes = discover_apps(input_path, hours_ago, max_recent_files)
        app_names = [name for _, name, _ in app_prefixes]
        if max_recent_files > 0:
            print(f"Discovered {len(app_names)} apps (top {max_recent_files} recent)")
        else:
            print(f"Discovered {len(app_names)} apps from last {hours_ago} hour(s)")
        if not app_names:
            print(f"No applications found modified in the last {hours_ago} hour(s). Nothing to extract.")
            elapsed = time.time() - start
            print(f"\nTotal time: {elapsed:.1f}s ({elapsed/60:.1f} minutes)")
            print(f"\nExtracted 0 job_ids from output files")
            return []  # Return empty list instead of None
        phase_b_spark_extract(app_names, None, output_path, hours_ago,
                              s3_mode=True, bucket=bucket, app_prefixes=app_prefixes,
                              spark=spark)
        # Extract job_ids from all files written
        job_ids = extract_job_ids_from_output_files(output_path)

    elapsed = time.time() - start
    print(f"\nTotal time: {elapsed:.1f}s ({elapsed/60:.1f} minutes)")

    # job_ids already extracted in each branch above (single_app reads specific file, others read all files)
    print(f"\nExtracted {len(job_ids) if job_ids else 0} job_ids from output files")
    if job_ids:
        for i, jid in enumerate(job_ids, 1):
            print(f"  {i}. {jid}")

    return job_ids
