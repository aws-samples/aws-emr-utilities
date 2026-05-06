#!/usr/bin/env python3
"""
Spark-based event log extractor — reads zstd-compressed event logs directly
from S3 using streaming decompression (no local staging required).

Falls back to local-disk decompression (Phase A) when --local-decompress is set.

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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO

sys.setrecursionlimit(10000)

import boto3
import zstandard as zstd


# ── S3 app discovery (shared by both modes) ─────────────────────────

def discover_apps(input_path, limit):
    """Discover application prefixes from S3. Returns [(s3_prefix, app_name, is_rolling)]."""
    parts = input_path.replace("s3://", "").split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    s3 = boto3.client("s3", region_name="us-east-1")
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
    app_prefixes = []

    for cp in resp.get("CommonPrefixes", []):
        p = cp["Prefix"]
        name = p.rstrip("/").rsplit("/", 1)[-1]
        if name.startswith("eventlog_v2_"):
            app_prefixes.append((p, name, True))
        elif name.startswith("application_"):
            app_prefixes.append((p, name, False))

    for obj in resp.get("Contents", []):
        k = obj["Key"]
        name = k.rsplit("/", 1)[-1]
        if name.startswith("application_") and not k.endswith("/"):
            app_prefixes.append((k, name, False))

    if not app_prefixes:
        dir_name = prefix.rstrip("/").rsplit("/", 1)[-1]
        if dir_name.startswith("eventlog_v2_"):
            app_prefixes.append((prefix, dir_name, True))
        elif dir_name.startswith("application_"):
            app_prefixes.append((prefix, dir_name, False))

    return bucket, app_prefixes[:limit]


def list_app_files(bucket, app_prefix, is_rolling):
    """List S3 keys for a single app's event log files."""
    s3 = boto3.client("s3", region_name="us-east-1")
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=app_prefix):
        for obj in page.get("Contents", []):
            k = obj["Key"]
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
                        yield (line,)

        lines_rdd = raw.rdd.mapPartitions(stream_decompress)
        lines_df = spark.createDataFrame(lines_rdd, ["line"])
        return spark.read.json(lines_df.select("line").rdd.map(lambda r: r[0]))
    else:
        # Single bare file — read directly from S3
        keys = list_app_files(bucket, app_prefix, False)
        if not keys:
            return None
        s3_paths = [f"s3://{bucket}/{k}" for k in keys]
        # Check if any files are compressed
        compressed = [p for p in s3_paths if any(p.endswith(ext) for ext in (".zstd", ".zst", ".gz", ".gzip", ".bz2"))]
        if not compressed:
            # Plain text — Spark can read directly
            return spark.read.json(s3_paths)
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
        return spark.read.json(spark.sparkContext.parallelize(all_lines, max(1, len(all_lines) // 50000)))


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


def phase_a_decompress(input_path, local_base, limit, workers=50):
    """Decompress all apps from S3 to local jsonl files — flat parallelism."""
    bucket, app_prefixes = discover_apps(input_path, limit)

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

def phase_b_spark_extract(app_names, local_base, output_path, limit,
                          s3_mode=False, bucket=None, app_prefixes=None):
    """Use Spark to extract metrics. Reads from local disk or directly from S3."""
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
    mode_label = "S3 streaming" if s3_mode else "local files"
    print(f"\nExtracting metrics from {total_apps} apps using Spark ({mode_label})", flush=True)

    # Build lookup for S3 mode
    app_s3_info = {}
    if s3_mode and app_prefixes:
        for pfx, name, is_rolling in app_prefixes:
            app_s3_info[name] = (pfx, is_rolling)

    # Infer schema once from the first app, reuse for all others
    schema = None
    if not s3_mode:
        for app_id in app_names[:limit]:
            p = os.path.join(local_base, app_id, "events.jsonl")
            if os.path.exists(p) and os.path.getsize(p) > 0:
                schema = spark.read.json("file://" + p).schema
                print(f"Schema inferred ({len(schema.fields)} fields), reusing for all apps")
                break

    for idx, app_id in enumerate(app_names[:limit], 1):
        print(f"  [{idx}/{total_apps}] Extracting {app_id}...", flush=True)
        try:
            if s3_mode:
                pfx, is_rolling = app_s3_info[app_id]
                df = read_app_from_s3(spark, bucket, pfx, app_id, is_rolling)
                if df is None:
                    print(f"  Skip {app_id}: no data")
                    continue
            else:
                jsonl_path = os.path.join(local_base, app_id, "events.jsonl")
                if not os.path.exists(jsonl_path) or os.path.getsize(jsonl_path) == 0:
                    print(f"  Skip {app_id}: no data")
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

                        job_id = spark_config.get("spark.job_id", spark_app_id)
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

            # Detect Task End Reason field structure
            ter_fields = []
            if "Task End Reason" in [f.name for f in task_ends.schema.fields]:
                ter_fields = [f.name for f in task_ends.schema["Task End Reason"].dataType.fields]
            has_reason = "Reason" in ter_fields

            task_reason_col = (
                F.col("`Task End Reason`.Reason") if has_reason
                else F.lit("Success")
            )

            task_status = task_ends.select(task_reason_col.alias("reason")).groupBy("reason").count().collect()
            completed_tasks = sum(r["count"] for r in task_status if r["reason"] == "Success")
            failed_tasks = sum(r["count"] for r in task_status if r["reason"] and ("Failed" in r["reason"] or "Exception" in r["reason"]))
            killed_tasks = sum(r["count"] for r in task_status if r["reason"] and "Killed" in r["reason"])

            # ── IO + Spill aggregation from TaskEnd ──────────────
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
                F.col("`Stage ID`").alias("stage_id"),
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

            job_start_rows = job_starts.select(
                F.col("`Job ID`").alias("job_id"),
                F.col("`Submission Time`").alias("submit_ts"),
                F.col("`Stage IDs`").alias("stage_ids"),
            ).collect()

            # Safely extract job group from Properties
            js_fields = [f.name for f in job_starts.schema.fields]
            job_group_map = {}
            if "Properties" in js_fields:
                try:
                    jg_rows = job_starts.select(
                        F.col("`Job ID`").alias("jid"),
                        F.col("Properties.`spark.jobGroup.id`").alias("jg"),
                    ).collect()
                    for r in jg_rows:
                        if r["jg"]:
                            job_group_map[r["jid"]] = r["jg"]
                except:
                    pass

            job_end_rows = job_ends.select(
                F.col("`Job ID`").alias("job_id"),
                F.col("`Completion Time`").alias("complete_ts"),
            ).collect()
            # Safely get Job Result
            je_fields = [f.name for f in job_ends.schema.fields]
            job_result_map = {}
            if "Job Result" in je_fields:
                for r in job_ends.select(F.col("`Job ID`").alias("jid"), F.col("`Job Result`.Result").alias("result")).collect():
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

            stage_cols = [
                    F.col("`Stage Info`.`Stage ID`").alias("stage_id"),
                    F.col("`Stage Info`.`Stage Name`").alias("stage_name"),
                    F.col("`Stage Info`.`Number of Tasks`").alias("num_tasks"),
                    F.col("`Stage Info`.`Submission Time`").alias("submit_ts"),
                    F.col("`Stage Info`.`Completion Time`").alias("complete_ts"),
            ]
            # Failure Reason may not exist in all event log versions
            stage_info_fields = [f.name for f in stage_completed.schema["Stage Info"].dataType.fields] if "Stage Info" in [f.name for f in stage_completed.schema.fields] else []
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

            sql_start_rows = sql_starts.select(
                F.col("executionId").alias("exec_id"),
                F.col("description"),
                F.col("physicalPlanDescription").alias("plan"),
                F.col("time").alias("start_time"),
            ).collect()

            # Also try to get details (stack trace) from SQL start events
            sql_detail_fields = [f.name for f in sql_starts.schema.fields]
            sql_details_map = {}
            if "details" in sql_detail_fields:
                for r in sql_starts.select(F.col("executionId").alias("eid"), F.col("details")).collect():
                    sql_details_map[r["eid"]] = r["details"] or ""

            sql_end_map = {}
            for r in sql_ends.select(F.col("executionId").alias("exec_id"), F.col("time").alias("end_time")).collect():
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
                "spark_config": spark_config,
            }

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
    parser.add_argument("--local-decompress", action="store_true",
                        help="Use local-disk decompression instead of S3 streaming")
    parser.add_argument("--single-app", action="store_true",
                        help="Treat --input as a single app S3 path (for parallel orchestration)")
    args = parser.parse_args()

    LOCAL_STAGING = "/tmp/spark_extractor_staging"

    print(f"\n{'='*60}")
    start = time.time()

    if args.single_app:
        # Single-app mode: --input points to one app's S3 prefix
        print("SPARK EXTRACTOR — single app")
        print(f"{'='*60}")
        parts = args.input.replace("s3://", "").rstrip("/").split("/", 1)
        bucket = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        name = prefix.rsplit("/", 1)[-1]
        is_rolling = name.startswith("eventlog_v2_")
        # For rolling logs, prefix must end with /; for bare files, keep as-is
        if is_rolling and not prefix.endswith("/"):
            prefix += "/"
        app_prefixes = [(prefix, name, is_rolling)]
        phase_b_spark_extract([name], None, args.output, 1,
                              s3_mode=True, bucket=bucket, app_prefixes=app_prefixes)
    elif args.local_decompress:
        print("SPARK EXTRACTOR — local decompress + Spark extract")
        print(f"{'='*60}")
        app_names = phase_a_decompress(args.input, LOCAL_STAGING, args.limit, args.decompress_workers)
        phase_b_spark_extract(app_names, LOCAL_STAGING, args.output, args.limit)
    else:
        print("SPARK EXTRACTOR — S3 streaming decompress")
        print(f"{'='*60}")
        bucket, app_prefixes = discover_apps(args.input, args.limit)
        app_names = [name for _, name, _ in app_prefixes]
        print(f"Discovered {len(app_names)} apps")
        phase_b_spark_extract(app_names, None, args.output, args.limit,
                              s3_mode=True, bucket=bucket, app_prefixes=app_prefixes)

    elapsed = time.time() - start
    print(f"\nTotal time: {elapsed:.1f}s ({elapsed/60:.1f} minutes)")
