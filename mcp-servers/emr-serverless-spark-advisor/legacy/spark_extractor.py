#!/usr/bin/env python3
"""
Two-phase event log extractor:
  Phase A: Python decompresses zstd files from S3 to local disk (parallel)
  Phase B: Spark reads decompressed JSON and extracts metrics (parallel)

Usage:
    python3 spark_extractor.py \
        --input s3://bucket/event-logs/ \
        --output s3://bucket/mcp-staging/run_id/ \
        --limit 10
"""

import argparse
import gzip
import bz2
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import BytesIO

import boto3
import zstandard as zstd


# ── Phase A: Python decompress ──────────────────────────────────────

def _sanitize_dotted_keys(line):
    """Convert dict fields with dotted/slashed keys to array-of-pairs for PySpark compatibility."""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return line
    changed = False
    for key, val in list(obj.items()):
        if isinstance(val, dict) and any("." in k or "/" in k for k in val):
            obj[key] = [[k, v] for k, v in val.items()]
            changed = True
        elif isinstance(val, dict):
            for k2, v2 in list(val.items()):
                if isinstance(v2, dict) and any("." in k or "/" in k for k in v2):
                    val[k2] = [[k, v] for k, v in v2.items()]
                    changed = True
    return json.dumps(obj, separators=(",", ":")) if changed else line


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


def phase_a_decompress(input_path, local_base, limit, workers=50):
    """Decompress all apps from S3 to local jsonl files — flat parallelism."""
    parts = input_path.replace("s3://", "").split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    s3 = boto3.client("s3", region_name="us-east-1")

    # List application directories (v2 format: eventlog_v2_*, v1 format: application_*)
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
    app_prefixes = []
    v1_files = []  # single-file v1 event logs
    for cp in resp.get("CommonPrefixes", []):
        p = cp["Prefix"]
        name = p.rstrip("/").rsplit("/", 1)[-1]
        if name.startswith("eventlog_v2_") or name.startswith("application_"):
            app_prefixes.append((p, name))

    # Check for v1 single-file event logs (not directories)
    for obj in resp.get("Contents", []):
        name = obj["Key"].rsplit("/", 1)[-1]
        if name.startswith("application_") and obj.get("Size", 0) > 0:
            v1_files.append((obj["Key"], name))

    # If no subdirectories found, check if the prefix itself is an app directory
    if not app_prefixes and not v1_files:
        dir_name = prefix.rstrip("/").rsplit("/", 1)[-1]
        if dir_name.startswith("eventlog_v2_") or dir_name.startswith("application_"):
            app_prefixes.append((prefix, dir_name))

    app_prefixes = app_prefixes[:limit]

    # List ALL files across all apps in one pass
    all_tasks = []  # (bucket, key, app_name)
    for app_prefix, app_name in app_prefixes:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=app_prefix):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                # v2 format: events_N files; v1 format: any file in the directory
                fname = k.rsplit("/", 1)[-1]
                if "/events_" in k or fname.startswith("application_") or not fname.startswith("."):
                    all_tasks.append((bucket, k, app_name))

    # Add v1 single-file event logs
    for key, name in v1_files[:max(0, limit - len(app_prefixes))]:
        all_tasks.append((bucket, key, name))

    print(f"Phase A: Decompressing {len(app_prefixes)} apps, {len(all_tasks)} files with {workers} threads")
    os.makedirs(local_base, exist_ok=True)

    # Collect decompressed text per app in memory
    app_buffers = {}
    app_counts = {}
    for _, app_name in app_prefixes:
        app_buffers[app_name] = []
        app_counts[app_name] = 0
    for _, app_name in v1_files[:max(0, limit - len(app_prefixes))]:
        app_buffers[app_name] = []
        app_counts[app_name] = 0

    start = time.time()

    # Flat parallelism — all files across all apps in one pool
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for app_name, text in pool.map(_decompress_one_file, all_tasks):
            if text:
                for line in text.splitlines():
                    line = line.strip()
                    if line:
                        line = _sanitize_dotted_keys(line)
                        app_buffers[app_name].append(line)
                        app_counts[app_name] += 1
            completed += 1
            if completed % 50 == 0 or completed == len(all_tasks):
                print(f"  Decompressing... {completed}/{len(all_tasks)} files", flush=True)

    # Write to local disk AND S3 staging.
    # EMR Serverless runs driver and executors in separate containers,
    # so Spark tasks cannot read file:// paths written by the driver.
    # S3 staging makes the decompressed data accessible to all workers.
    s3_upload = boto3.client("s3", region_name="us-east-1")
    staging_prefix = local_base.lstrip("/")
    for app_name, lines in app_buffers.items():
        content = "\n".join(lines) + "\n" if lines else ""
        # Local disk (for non-EMR-Serverless environments)
        d = os.path.join(local_base, app_name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.write(content)
        # S3 staging (for EMR Serverless)
        if content:
            try:
                s3_upload.put_object(
                    Bucket=bucket, Key=f"{staging_prefix}/{app_name}/events.jsonl",
                    Body=content.encode("utf-8"), ContentType="application/x-ndjson")
            except Exception as e:
                print(f"  Warning: S3 staging upload for {app_name}: {e}", file=sys.stderr)

    elapsed = time.time() - start
    total_lines = sum(app_counts.values())
    for app_name, count in app_counts.items():
        print(f"  ✓ {app_name}: {count} events")
    print(f"Phase A done: {len(app_prefixes)} apps, {total_lines} events in {elapsed:.1f}s")
    return list(app_counts.keys()), bucket, staging_prefix


# ── Phase B: Spark extraction ────────────────────────────────────────

def phase_b_spark_extract(app_names, local_base, output_path, limit, s3_staging_bucket=None, s3_staging_prefix=None):
    """Use Spark to read decompressed JSON and extract metrics."""
    from pyspark.sql import SparkSession, functions as F

    spark = (
        SparkSession.builder
        .appName("SparkEventLogExtractor")
        .config("spark.sql.adaptive.enabled", "true")
        .config("spark.ui.showConsoleProgress", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")

    GB = 1024 ** 3
    results = []
    total_apps = min(limit, len(app_names))
    print(f"\nPhase B: Extracting metrics from {total_apps} apps using Spark", flush=True)

    # Prefer S3 staging (works on EMR Serverless), fall back to local disk
    def _read_path(app_id):
        if s3_staging_bucket and s3_staging_prefix:
            return f"s3://{s3_staging_bucket}/{s3_staging_prefix}/{app_id}/events.jsonl"
        return "file://" + os.path.join(local_base, app_id, "events.jsonl")

    # Infer schema once from the first app, reuse for all others
    schema = None
    first_path = None
    for app_id in app_names[:limit]:
        first_path = _read_path(app_id)
        break
    if first_path:
        schema = spark.read.json(first_path).schema
        print(f"Schema inferred ({len(schema.fields)} fields), reusing for all apps")

    for idx, app_id in enumerate(app_names[:limit], 1):
        read_path = _read_path(app_id)

        print(f"  [{idx}/{min(limit, len(app_names))}] Extracting {app_id}...", flush=True)
        try:
            if schema:
                df = spark.read.schema(schema).json(read_path)
            else:
                df = spark.read.json(read_path)
            df.cache()
            total_events = df.count()

            # ── App Info ─────────────────────────────────────────
            app_start_row = df.filter(F.col("Event") == "SparkListenerApplicationStart").first()
            app_end_row = df.filter(F.col("Event") == "SparkListenerApplicationEnd").first()

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

            env_row = df.filter(F.col("Event") == "SparkListenerEnvironmentUpdate").first()
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

                        job_id = spark_config.get("spark.job_id", spark_app_id)
                        cluster_id = spark_config.get("spark.emr_cluster_id", "N/A")
                except Exception:
                    pass

            # ── IO + Spill aggregation from TaskEnd ──────────────
            task_ends = df.filter(F.col("Event") == "SparkListenerTaskEnd")

            io_agg = task_ends.select(
                F.col("`Task Metrics`.`Executor Run Time`").alias("run_time"),
                F.col("`Task Metrics`.`Input Metrics`.`Bytes Read`").alias("input_bytes"),
                F.col("`Task Metrics`.`Output Metrics`.`Bytes Written`").alias("output_bytes"),
                F.col("`Task Metrics`.`Shuffle Read Metrics`.`Remote Bytes Read`").alias("shuffle_remote"),
                F.col("`Task Metrics`.`Shuffle Read Metrics`.`Local Bytes Read`").alias("shuffle_local"),
                F.col("`Task Metrics`.`Shuffle Write Metrics`.`Shuffle Bytes Written`").alias("shuffle_write"),
                F.col("`Task Metrics`.`Memory Bytes Spilled`").alias("mem_spill"),
                F.col("`Task Metrics`.`Disk Bytes Spilled`").alias("disk_spill"),
                F.col("`Task Info`.`Executor ID`").alias("exec_id"),
                F.col("`Task Executor Metrics`.JVMHeapMemory").alias("jvm_heap"),
                F.col("`Task Metrics`.`JVM GC Time`").alias("gc_time"),
                F.col("`Task Metrics`.`Shuffle Read Metrics`.`Fetch Wait Time`").alias("fetch_wait_time"),
                F.col("`Task Info`.`Failed`").alias("task_failed"),
            )

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
                F.coalesce(F.sum("gc_time"), F.lit(0)).alias("total_gc_time"),
                F.coalesce(F.sum("fetch_wait_time"), F.lit(0)).alias("total_fetch_wait_time"),
                F.sum(F.when(F.col("task_failed") == True, 1).otherwise(0)).alias("failed_tasks"),
            ).first()

            task_count = agg_result["task_count"]
            failed_tasks = int(agg_result["failed_tasks"] or 0)
            total_input = agg_result["total_input"]
            total_output = agg_result["total_output"]
            total_shuffle_read = agg_result["total_shuffle_remote"] + agg_result["total_shuffle_local"]
            total_shuffle_write = agg_result["total_shuffle_write"]
            total_run_time_ms = float(agg_result["total_run_time"] or 0)
            total_gc_time_ms = float(agg_result["total_gc_time"] or 0)
            total_fetch_wait_ms = float(agg_result["total_fetch_wait_time"] or 0)
            gc_pct = round(total_gc_time_ms / total_run_time_ms * 100, 2) if total_run_time_ms > 0 else 0
            fetch_wait_pct = round(total_fetch_wait_ms / total_run_time_ms * 100, 2) if total_run_time_ms > 0 else 0

            # ── Job details ──────────────────────────────────────
            job_starts = df.filter(F.col("Event") == "SparkListenerJobStart")
            job_ends = df.filter(F.col("Event") == "SparkListenerJobEnd")
            job_start_rows = job_starts.select(
                F.col("`Job ID`").alias("job_id"),
                F.col("`Submission Time`").alias("submit_time"),
            ).collect()
            job_end_rows = job_ends.select(
                F.col("`Job ID`").alias("job_id"),
                F.col("`Completion Time`").alias("end_time"),
                F.col("`Job Result`.Result").alias("result"),
            ).collect()
            job_end_map = {r["job_id"]: r for r in job_end_rows}
            job_details = []
            for r in job_start_rows:
                jid = r["job_id"]
                end_r = job_end_map.get(jid)
                status = "RUNNING"
                failure_reason = None
                if end_r:
                    status = "SUCCESS" if end_r["result"] == "JobSucceeded" else "FAILED"
                job_details.append({
                    "job_id": int(jid),
                    "status": status,
                    "failure_reason": failure_reason,
                })

            # ── Executor summary ─────────────────────────────────
            exec_agg = (
                io_agg
                .filter((F.col("exec_id").isNotNull()) & (F.col("exec_id") != "driver"))
                .groupBy("exec_id")
                .agg(
                    F.count("*").alias("tasks"),
                    F.coalesce(F.sum("run_time"), F.lit(0)).alias("total_run_time_ms"),
                    F.coalesce(F.max("jvm_heap"), F.lit(0)).alias("peak_jvm_heap"),
                )
            )

            exec_added = (
                df.filter(F.col("Event") == "SparkListenerExecutorAdded")
                .select(
                    F.col("`Executor ID`").alias("added_id"),
                    F.col("Timestamp").alias("add_ts"),
                    F.col("`Executor Info`.`Total Cores`").alias("cores"),
                )
            )
            exec_removed = (
                df.filter(F.col("Event") == "SparkListenerExecutorRemoved")
                .select(
                    F.col("`Executor ID`").alias("removed_id"),
                    F.col("Timestamp").alias("remove_ts"),
                )
            )

            exec_full = (
                exec_added
                .join(exec_agg, exec_added["added_id"] == exec_agg["exec_id"], "left")
                .join(exec_removed, exec_added["added_id"] == exec_removed["removed_id"], "left")
                .withColumn("cores", F.coalesce(F.col("cores"), F.lit(executor_cores_cfg)))
                .withColumn("tasks", F.coalesce(F.col("tasks"), F.lit(0)))
                .withColumn("total_run_time_ms", F.coalesce(F.col("total_run_time_ms"), F.lit(0)))
                .withColumn("peak_jvm_heap", F.coalesce(F.col("peak_jvm_heap"), F.lit(0)))
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
            total_core_hours = max(total_cores * total_uptime, 1)
            idle_pct = max(0, round((1 - total_task_time_hours / total_core_hours) * 100, 2))

            executor_memory_gb = executor_memory_mb / 1024

            # ── Per-stage details ─────────────────────────────────
            stage_submitted = df.filter(F.col("Event") == "SparkListenerStageSubmitted")
            stage_completed = df.filter(F.col("Event") == "SparkListenerStageCompleted")

            stage_cols = [
                    F.col("`Stage Info`.`Stage ID`").alias("stage_id"),
                    F.col("`Stage Info`.`Stage Name`").alias("stage_name"),
                    F.col("`Stage Info`.`Number of Tasks`").alias("num_tasks"),
                    F.col("`Stage Info`.`Submission Time`").alias("submit_ts"),
                    F.col("`Stage Info`.`Completion Time`").alias("complete_ts"),
            ]
            # Failure Reason may not exist in all event log versions
            stage_info_fields = [f.name for f in stage_completed.schema["`Stage Info`"].dataType.fields] if "`Stage Info`" in [f.name for f in stage_completed.schema.fields] else []
            if "Failure Reason" in stage_info_fields:
                stage_cols.append(F.col("`Stage Info`.`Failure Reason`").alias("failure_reason"))
            else:
                stage_cols.append(F.lit(None).cast("string").alias("failure_reason"))

            stage_times = (
                stage_completed.select(*stage_cols)
                .withColumn("duration_ms", F.col("complete_ts") - F.col("submit_ts"))
            )

            # Per-stage IO from TaskEnd
            stage_io = (
                task_ends.select(
                    F.col("`Stage ID`").alias("stage_id"),
                    F.col("`Task Metrics`.`Input Metrics`.`Bytes Read`").alias("input_bytes"),
                    F.col("`Task Metrics`.`Output Metrics`.`Bytes Written`").alias("output_bytes"),
                    F.col("`Task Metrics`.`Shuffle Read Metrics`.`Remote Bytes Read`").alias("shuffle_remote"),
                    F.col("`Task Metrics`.`Shuffle Read Metrics`.`Local Bytes Read`").alias("shuffle_local"),
                    F.col("`Task Metrics`.`Shuffle Write Metrics`.`Shuffle Bytes Written`").alias("shuffle_write"),
                    F.col("`Task Metrics`.`Memory Bytes Spilled`").alias("mem_spill"),
                    F.col("`Task Metrics`.`Disk Bytes Spilled`").alias("disk_spill"),
                    F.col("`Task Metrics`.`Executor Run Time`").alias("run_time"),
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
            for r in stage_details_rows:
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

            sql_start_rows = sql_starts.select(
                F.col("executionId").alias("exec_id"),
                F.col("description"),
                F.col("physicalPlanDescription").alias("plan"),
                F.col("time").alias("start_time"),
            ).collect()

            sql_end_map = {}
            for r in sql_ends.select(F.col("executionId").alias("exec_id"), F.col("time").alias("end_time")).collect():
                sql_end_map[r["exec_id"]] = r["end_time"]

            sql_executions = []
            for r in sql_start_rows:
                eid = r["exec_id"]
                end_t = sql_end_map.get(eid)
                dur = (end_t - r["start_time"]) if end_t and r["start_time"] else None
                sql_executions.append({
                    "execution_id": int(eid),
                    "description": (r["description"] or "")[:200],
                    "duration_sec": round(dur / 1000, 1) if dur else None,
                    "physical_plan": r["plan"] or "",
                })

            # ── Build output ─────────────────────────────────────
            task_stage_output = {
                "application_id": app_id,
                "extraction_timestamp": datetime.now().isoformat(),
                "extraction_engine": "pyspark",
                "event_count": total_events,
                "application_info": {
                    "job_id": job_id, "cluster_id": cluster_id,
                    "application_name": app_name, "app_id": spark_app_id,
                    "application_start_time": datetime.fromtimestamp(start_ts / 1000).isoformat() if start_ts else None,
                    "application_end_time": datetime.fromtimestamp(end_ts / 1000).isoformat() if end_ts else None,
                    "total_run_duration_minutes": duration_minutes,
                    "total_run_duration_hours": duration_hours,
                },
                "task_summary": {"total_tasks": task_count, "failed_tasks": failed_tasks, "killed_tasks": 0},
                "job_details": {"jobs": job_details},
                "stage_summary": {"stages": stage_details, "total_stages": len(stage_details)},
                "executor_summary": {
                    "total_executors": total_executors,
                    "active_executors": active_count,
                    "dead_executors": dead_count,
                    "total_cores": total_cores,
                    "total_uptime_hours": total_uptime,
                    "max_peak_memory_gb": float(exec_summary["max_peak_gb"] or 0),
                    "avg_peak_memory_gb": float(exec_summary["avg_peak_gb"] or 0),
                    "avg_memory_utilization_percent": avg_mem_util,
                    "avg_cpu_utilization_percent": avg_cpu_util,
                    "idle_core_percentage": idle_pct,
                    "total_cost_factor": cost_factor,
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
                        "shuffle_fetch_wait_percent": fetch_wait_pct,
                        "gc_time_percent": gc_pct,
                    }
                },
                "spill_summary": {
                    "total_memory_spilled_bytes": int(agg_result["total_mem_spill"]),
                    "total_memory_spilled_gb": round(agg_result["total_mem_spill"] / GB, 2),
                    "total_disk_spilled_bytes": int(agg_result["total_disk_spill"]),
                    "total_disk_spilled_gb": round(agg_result["total_disk_spill"] / GB, 2),
                    "tasks_with_memory_spill": int(agg_result["tasks_with_mem_spill"] or 0),
                    "tasks_with_disk_spill": int(agg_result["tasks_with_disk_spill"] or 0),
                    "tasks_analyzed": task_count,
                },
                "total_cost_factor": cost_factor,
                "executor_timeline": executor_timeline,
                "sql_executions": sql_executions,
                "spark_config": spark_config,
            }

            config_output = {
                "application_id": app_id,
                "extraction_timestamp": datetime.now().isoformat(),
                "cluster_id": cluster_id, "job_id": job_id,
                "application_name": app_name, "app_id": spark_app_id,
                "application_start_time": task_stage_output["application_info"]["application_start_time"],
                "application_end_time": task_stage_output["application_info"]["application_end_time"],
                "total_run_duration_minutes": duration_minutes,
                "total_run_duration_hours": duration_hours,
                "total_cost_factor": cost_factor,
                "spark_configuration": spark_config,
            }

            results.append((app_id, task_stage_output, config_output))
            df.unpersist()
            print(f"  ✓ {app_id}: {task_count} tasks, {round(total_input/GB,1)} GB input, {total_executors} executors")

        except Exception as e:
            print(f"  ✗ {app_id}: {e}", file=sys.stderr)
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

    print(f"✅ Extraction complete: {len(results)} applications")
    spark.stop()


# ── Main ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Spark-based event log extractor")
    parser.add_argument("--input", required=True, help="S3 path to event logs")
    parser.add_argument("--output", required=True, help="Output path for extracted metrics")
    parser.add_argument("--limit", type=int, default=100, help="Max apps")
    parser.add_argument("--decompress-workers", type=int, default=50, help="Parallel download threads")
    args = parser.parse_args()

    LOCAL_STAGING = "/tmp/spark_extractor_staging"

    print(f"\n{'='*60}")
    print("SPARK EVENT LOG EXTRACTOR (Python decompress + Spark extract)")
    print(f"{'='*60}")
    start = time.time()

    # Phase A: Python decompress
    app_names, staging_bucket, staging_prefix = phase_a_decompress(args.input, LOCAL_STAGING, args.limit, args.decompress_workers)

    # Phase B: Spark extract
    phase_b_spark_extract(app_names, LOCAL_STAGING, args.output, args.limit,
                          s3_staging_bucket=staging_bucket, s3_staging_prefix=staging_prefix)

    elapsed = time.time() - start
    print(f"\nTotal time: {elapsed:.1f}s ({elapsed/60:.1f} minutes)")
