#!/usr/bin/env python3
"""
EMR Serverless Recommender - Dual Mode with Local/S3 Support
Supports both local filesystem and S3 for input/output.
"""

import sys
import json
import glob
from pathlib import Path
from typing import List, Dict, Tuple
import pandas as pd

# Import centralized logging configuration
try:
    from logging_config import setup_logging
    log = setup_logging(name="dual-mode-recommender")
except ImportError:
    # Fallback to basic logging if logging_config not available
    import logging
    import time
    logging.basicConfig(
        format="%(asctime)s UTC %(levelname)-5s [%(name)s]  %(message)s",
        datefmt='%Y-%m-%d %H:%M:%S',
        level=logging.INFO,
    )
    logging.Formatter.converter = time.gmtime  # Force UTC
    log = logging.getLogger("dual-mode-recommender")

# Try to import S3 support
try:
    import boto3
    S3_AVAILABLE = True
except ImportError:
    S3_AVAILABLE = False
    log.warning("boto3 not available - S3 support disabled")


def is_s3_path(path: str) -> bool:
    """Check if path is S3."""
    return path.startswith('s3://')


def load_json_files(path: str, limit: int = None) -> List[Dict]:
    """Load JSON files from S3 or local filesystem. Limit parameter is ignored - all files are processed."""
    all_data = []

    if is_s3_path(path):
        if not S3_AVAILABLE:
            raise RuntimeError("boto3 required for S3 paths. Install: pip install boto3")

        # S3 path
        bucket, prefix = path.replace('s3://', '').split('/', 1)
        prefix = prefix.rstrip('/') + '/task_stage_summary/'

        s3_client = boto3.client('s3')
        log.info("Loading from S3: s3://%s/%s", bucket, prefix)

        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            if 'Contents' not in page:
                continue

            for obj in page['Contents']:
                key = obj['Key']
                if not key.endswith('.json'):
                    continue

                try:
                    response = s3_client.get_object(Bucket=bucket, Key=key)
                    data = json.loads(response['Body'].read())
                    all_data.append(data)
                except Exception as e:
                    log.debug("Skipping %s: %s", key, e)
    else:
        # Local filesystem
        search_path = Path(path) / 'task_stage_summary' / '*.json'
        log.info("Loading from local: %s", search_path)

        json_files = glob.glob(str(search_path))
        for json_file in json_files:
            try:
                with open(json_file) as f:
                    data = json.load(f)
                    all_data.append(data)
            except Exception as e:
                log.debug("Skipping %s: %s", json_file, e)

    log.info("Loaded %d JSON files", len(all_data))
    return all_data


def _gb_to_gib(gb: float) -> float:
    return round(gb * 0.931323, 2)


def _calculate_shuffle_ratio(input_gb, read_gb, write_gb) -> float:
    if input_gb == 0:
        return 0.0
    return round((read_gb + write_gb) / input_gb * 100.0, 2)


def _select_worker_type(input_gb: float, shuffle_ratio: float,
                        mem_pct: float = 60.0, spill_gb: float = 0.0,
                        cpu_pct: float = 50.0, orig_mem_mb: int = 0,
                        max_peak_mem_gb: float = 0, orig_cores: int = 0) -> Tuple[str, Dict]:
    # EMR Serverless memory ranges per vCPU size
    WORKER_RANGES = {
        "Small":  {"vcpu": 4,  "min_mem": 8,  "max_mem": 27, "mem_step": 1},
        "Medium": {"vcpu": 8,  "min_mem": 16, "max_mem": 54, "mem_step": 4},
        "Large":  {"vcpu": 16, "min_mem": 64, "max_mem": 108, "mem_step": 8},
    }

    # Select worker size based on data volume and spill (workload properties)
    if input_gb > 2048 or (spill_gb > 100):
        size = "Large"
    elif input_gb > 500 or (spill_gb > 10) or shuffle_ratio > 50:
        size = "Large"
    else:
        size = "Small"

    r = WORKER_RANGES[size]

    # Memory sizing: peak memory per core + 50% headroom
    if max_peak_mem_gb > 0 and orig_cores > 0:
        peak_per_core = max_peak_mem_gb / orig_cores
        mem_needed = int(peak_per_core * 1.5 * r["vcpu"])
        mem = max(r["min_mem"], min(r["max_mem"], mem_needed))
    elif spill_gb > 0:
        mem = r["max_mem"]
    else:
        mem = r["min_mem"]

    # Round UP to valid EMR Serverless memory increment (preserve headroom)
    step = r["mem_step"]
    mem = min(r["max_mem"], max(r["min_mem"], r["min_mem"] + (-(-(mem - r["min_mem"]) // step)) * step))
    # If within one step of max, use max (e.g. 108GB for Large)
    if mem + step > r["max_mem"]:
        mem = r["max_mem"]

    return size, {"vcpu": r["vcpu"], "memory": mem}


def _compute_exec_limits(input_gb: float, vcpu: int, partitions: int = 0,
                        mem_pct: float = 60.0, cpu_pct: float = 50.0,
                        idle_pct: float = 50.0, spill_gb: float = 0.0,
                        mode: str = "cost",
                        orig_executors: int = 0, orig_cores: int = 0,
                        total_task_exec_hours: float = 0,
                        duration_hours: float = 0,
                        stages: list = None) -> Tuple[int, int]:
    req_input = max(1, int(input_gb / 100))
    part_divisor = 4 if mode == "cost" else 3
    req_part = max(1, int((partitions / vcpu) / part_divisor)) if partitions > 0 else 0
    base_req = max(req_input, req_part)

    # Pressure factor based on spill (stable workload property)
    spill_ratio = (spill_gb / max(input_gb, 1)) * 100
    spill_pressure = min(40, spill_ratio * 0.2)
    if mode == "performance":
        factor = 1.0 + (spill_pressure / 100) * 0.5
    else:
        factor = 0.8 + (spill_pressure / 100) * 0.5

    max_exec = max(2, int(base_req * factor))

    # --- Work-based sizing (primary signal) ---
    # Uses actual observed task execution time to determine how many cores
    # are needed, independent of the original cluster size.
    if total_task_exec_hours > 0 and duration_hours > 0:
        if mode == "cost":
            # Allow 3x original duration — trade time for fewer executors
            target_hours = duration_hours * 3
        else:
            # Match original duration
            target_hours = duration_hours

        cores_needed = total_task_exec_hours / target_hours
        work_exec = max(2, int(cores_needed / vcpu))

        # Efficiency discount: when idle% is very high, the original run was
        # over-parallelized — task overhead (scheduling, serialization, GC)
        # inflates total_task_exec_hours. Fewer executors = less overhead.
        if idle_pct > 70:
            efficiency = max(0.3, 1.0 - (idle_pct - 50) / 100)
            work_exec = max(2, int(work_exec * efficiency))

        max_exec = max(max_exec, work_exec)

    # --- Fallback: original-run floor (only when task data is missing) ---
    elif orig_executors > 0 and orig_cores > 0:
        orig_total_cores = orig_executors * orig_cores
        if mode == "performance":
            equiv = max(2, int(orig_total_cores * 1.6 / vcpu))
        else:
            util_factor = max(0.3, min(1.0, cpu_pct / 100.0))
            equiv = max(2, int(orig_total_cores * util_factor / vcpu))
        max_exec = max(max_exec, equiv)

    min_exec = max(1, max_exec // 2)

    return max_exec, min_exec


def _calculate_executor_disk(shuffle_write_gb: float, disk_spill_gb: float,
                             memory_spill_gb: float, max_executors: int) -> str:
    total_shuffle_per_exec = shuffle_write_gb / max(max_executors, 1)
    total_spill_per_exec = (disk_spill_gb + memory_spill_gb) / max(max_executors, 1)
    estimated_gb = (total_shuffle_per_exec + total_spill_per_exec) * 1.5
    disk_gb = max(500, min(2000, int(estimated_gb)))
    return f"{disk_gb}G"


def _max_partition_bytes(input_gb: float) -> str:
    if input_gb >= 1024:
        return "512m"
    return "128m"


def _get_timeout_configs(input_gb: float, duration_hours: float) -> Dict[str, str]:
    import math
    if math.isnan(input_gb): input_gb = 0
    if math.isnan(duration_hours): duration_hours = 0
    base_timeout = 600
    data_factor = int(input_gb / 1000) * 60
    duration_factor = int(duration_hours) * 120 if duration_hours else 0
    shuffle_timeout = min(max(base_timeout + data_factor + duration_factor, 600), 1800)
    network_timeout = shuffle_timeout * 2

    return {
        "spark.network.timeout": f"{network_timeout}s",
        "spark.shuffle.io.connectionTimeout": f"{shuffle_timeout}s",
    }


def _get_s3_retry_configs() -> Dict[str, str]:
    return {
        "spark.hadoop.fs.s3a.attempts.maximum": "15",
    }


def _get_iceberg_configs() -> Dict[str, str]:
    return {
        "spark.sql.catalog.spark_catalog": "org.apache.iceberg.spark.SparkSessionCatalog",
        "spark.sql.catalog.spark_catalog.type": "hive",
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
    }


def _get_openlineage_configs() -> Dict[str, str]:
    return {
        "spark.openlineage.parentRunId": "",
        "spark.openlineage.parentJobName": "",
        "spark.openlineage.parentJobNamespace": "",
    }


def generate_dual_recommendations(input_path: str, limit: int = None,
                                  target_partition_size_mib: int = 1024,
                                  serverless_storage: bool = False) -> Tuple[List[Dict], List[Dict]]:
    """Generate cost, performance, and IO-optimized recommendations. Limit parameter is ignored - all applications are processed."""

    # Load metrics (limit parameter kept for backward compatibility but ignored)
    all_data = load_json_files(input_path, limit)

    # Convert to DataFrame
    flattened = []
    for data in all_data:
        # Handle both old format (io/utilization) and new format (io_summary/executor_summary)
        app_info = data.get('application_info', {})
        io_data = data.get('io', data.get('io_summary', {}).get('application_level', {}))
        util_data = data.get('utilization', data.get('executor_summary', {}))
        spill_data = data.get('spill_summary', {})

        flat = {
            'application_id': data.get('application_id', app_info.get('app_id')),
            'application_name': data.get('application_name', app_info.get('application_name')),
            'job_id': app_info.get('job_id'),
            'total_run_duration_hours': data.get('total_run_duration_hours', app_info.get('total_run_duration_hours')),
            'io_total_input_gb': io_data.get('total_input_gb'),
            'io_total_shuffle_read_gb': io_data.get('total_shuffle_read_gb'),
            'io_total_shuffle_write_gb': io_data.get('total_shuffle_write_gb'),
            'avg_memory_utilization_percent': util_data.get('avg_memory_utilization_percent'),
            'avg_cpu_utilization_percent': util_data.get('avg_cpu_utilization_percent'),
            'idle_core_percentage': util_data.get('idle_core_percentage'),
            'total_memory_spilled_gb': spill_data.get('total_memory_spilled_gb'),
            'total_disk_spilled_gb': spill_data.get('total_disk_spilled_gb'),
            'max_stage_tasks': max((s.get('num_tasks', 0) for s in data.get('stage_summary', {}).get('stages', [])), default=0),
            'max_peak_memory_gb': util_data.get('max_peak_memory_gb', 0),
            'orig_executor_cores': int(data.get('spark_config', {}).get('spark.executor.cores', 0) or 0),
            'orig_total_executors': int(util_data.get('total_executors', 0) or 0),
            'total_task_execution_hours': util_data.get('total_task_execution_hours', 0),
            'max_stage_shuffle_write_gb': data.get('shuffle_data_summary', {}).get('max_stage_shuffle_write_gb', 0),
            'shuffle_fetch_wait_percent': io_data.get('shuffle_fetch_wait_percent', 0),
        }
        flattened.append(flat)

    # Check if we have any data
    if not flattened:
        log.error("No data found in input path. Please check the path and data format.")
        return [], []

    # Debug: Log first record structure
    if flattened and log.isEnabledFor(logging.DEBUG):
        log.debug("Sample flattened record keys: %s", list(flattened[0].keys()))
        log.debug("Sample flattened record: %s", flattened[0])

    df = pd.DataFrame(flattened)

    # Define required columns with defaults
    required_columns = {
        'io_total_input_gb': 0.0,
        'io_total_shuffle_read_gb': 0.0,
        'io_total_shuffle_write_gb': 0.0,
        'avg_memory_utilization_percent': 60.0,
        'avg_cpu_utilization_percent': 50.0,
        'idle_core_percentage': 50.0,
        'total_memory_spilled_gb': 0.0,
        'total_disk_spilled_gb': 0.0,
        'max_stage_tasks': 0,
        'max_peak_memory_gb': 0.0,
        'orig_executor_cores': 0,
        'orig_total_executors': 0,
        'total_task_execution_hours': 0.0,
        'max_stage_shuffle_write_gb': 0.0,
        'shuffle_fetch_wait_percent': 0.0,
        'total_run_duration_hours': 0.0,
    }

    # Ensure all required columns exist
    for col, default_val in required_columns.items():
        if col not in df.columns:
            log.warning("Column '%s' not found in data, using default value: %s", col, default_val)
            df[col] = default_val

    # Sanitize NaN values - replace with 0 for numeric columns
    df = df.fillna(0)

    # Separate apps with and without data (include shuffle-only workloads as "with data")
    has_data = (df['io_total_input_gb'] > 0) | (df['io_total_shuffle_read_gb'] > 0) | (df['io_total_shuffle_write_gb'] > 0)
    df_with_data = df[has_data].sort_values('io_total_input_gb', ascending=False)
    df_no_data = df[~has_data]

    log.info("Processing %d applications with data, %d with no input data", len(df_with_data), len(df_no_data))

    cost_recs = []
    perf_recs = []

    # Process applications with input data
    for _, row in df_with_data.iterrows():
        app_id = row.get('application_id', 'N/A')
        name = row.get('application_name', 'N/A')
        job_id = row.get('job_id')
        duration = float(row.get('total_run_duration_hours', 0) or 0)
        i_in_gb = float(row.get('io_total_input_gb', 0) or 0)
        s_in_gb = float(row.get('io_total_shuffle_read_gb', 0) or 0)
        s_out_gb = float(row.get('io_total_shuffle_write_gb', 0) or 0)

        mem_pct = float(row.get('avg_memory_utilization_percent', 60.0) or 60.0)
        cpu_pct = float(row.get('avg_cpu_utilization_percent', 50.0) or 50.0)
        idle_pct = float(row.get('idle_core_percentage', 50.0) or 50.0)
        spill_gb = float(row.get('total_memory_spilled_gb', 0.0) or 0.0)
        disk_spill_gb = float(row.get('total_disk_spilled_gb', 0.0) or 0.0)
        max_stage_tasks = int(row.get('max_stage_tasks', 0) or 0)
        max_peak_mem_gb = float(row.get('max_peak_memory_gb', 0) or 0)
        orig_cores = int(row.get('orig_executor_cores', 0) or 0)
        orig_executors = int(row.get('orig_total_executors', 0) or 0)
        total_task_exec_hours = float(row.get('total_task_execution_hours', 0) or 0)
        max_stage_shuf_write = float(row.get('max_stage_shuffle_write_gb', 0) or 0)
        shuffle_fetch_wait_pct = float(row.get('shuffle_fetch_wait_percent', 0) or 0)

        sh_ratio = _calculate_shuffle_ratio(i_in_gb, s_in_gb, s_out_gb)
        worker_type, worker_cfg = _select_worker_type(i_in_gb, sh_ratio, mem_pct, spill_gb, cpu_pct,
                                                      max_peak_mem_gb=max_peak_mem_gb, orig_cores=orig_cores)

        shuffle_data_gb = max(s_in_gb, s_out_gb)
        shuffle_bytes = shuffle_data_gb * 1024 * 1024 * 1024
        has_shuffle = shuffle_data_gb > 0

        # Custom partition calculation
        def auto_tune_custom(shuffle_bytes, max_executors):
            target_mib = target_partition_size_mib
            if shuffle_bytes > 0:
                partitions = max(2, int((shuffle_bytes / (target_mib * 1024 * 1024)) + 0.5))
            else:
                # Scale by input size (128MB per partition) instead of flat 200
                partitions = max(2, min(200, int(i_in_gb / 0.128)))
            if partitions % 2 != 0:
                partitions += 1
            return partitions, target_mib

        # Cost-optimized
        max_exec_cost_init, min_exec_cost = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], 0, mem_pct, cpu_pct, idle_pct, spill_gb, mode="cost",
            orig_executors=orig_executors, orig_cores=orig_cores,
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration,
        )
        sp_cost, target_mib_cost = auto_tune_custom(shuffle_bytes, max_exec_cost_init)
        max_exec_cost, min_exec_cost = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], sp_cost, mem_pct, cpu_pct, idle_pct, spill_gb, mode="cost",
            orig_executors=orig_executors, orig_cores=orig_cores,
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration,
        )
        executor_disk_cost = _calculate_executor_disk(s_out_gb, disk_spill_gb, spill_gb, max_exec_cost)

        # Performance-optimized
        max_exec_perf_init, min_exec_perf = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], 0, mem_pct, cpu_pct, idle_pct, spill_gb, mode="performance",
            orig_executors=orig_executors, orig_cores=orig_cores,
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration,
        )
        sp_perf, target_mib_perf = auto_tune_custom(shuffle_bytes, max_exec_perf_init)
        max_exec_perf, min_exec_perf = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], sp_perf, mem_pct, cpu_pct, idle_pct, spill_gb, mode="performance",
            orig_executors=orig_executors, orig_cores=orig_cores,
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration,
        )
        executor_disk_perf = _calculate_executor_disk(s_out_gb, disk_spill_gb, spill_gb, max_exec_perf)

        # Build base metrics
        base_metrics = {
            "input_gb": round(i_in_gb, 2),
            "input_gib": _gb_to_gib(i_in_gb),
            "shuffle_read_gb": round(s_in_gb, 2),
            "shuffle_write_gb": round(s_out_gb, 2),
            "shuffle_total_gb": round(s_in_gb + s_out_gb, 2),
            "shuffle_ratio_percent": sh_ratio,
            "duration_hours": round(duration, 2),
            "avg_memory_utilization_percent": round(mem_pct, 2),
            "avg_cpu_utilization_percent": round(cpu_pct, 2),
            "idle_core_percentage": round(idle_pct, 2),
            "total_memory_spilled_gb": round(spill_gb, 2),
        }

        # Build Spark configs
        def _driver_sizing(partitions, max_exec, shuffle_gb):
            """Scale driver based on coordination overhead."""
            if partitions > 10000 or max_exec > 500 or shuffle_gb > 10000:
                return 16, 108
            elif partitions > 2000 or max_exec > 100 or shuffle_gb > 2000:
                return 8, 54
            elif partitions > 500 or max_exec > 50 or shuffle_gb > 500:
                return 4, 27
            else:
                return 4, 14

        def build_spark_cfg(max_exec, min_exec, sp, executor_disk, vcpu_override=None, mem_override=None):
            d_cores, d_mem = _driver_sizing(sp, max_exec, s_in_gb + s_out_gb)
            vcpu = vcpu_override or worker_cfg["vcpu"]
            mem = mem_override or worker_cfg["memory"]
            cfg = {
                "spark.driver.cores": str(d_cores),
                "spark.driver.memory": f"{d_mem}G",
                "spark.executor.cores": str(vcpu),
                "spark.executor.memory": f"{mem}g",
                "spark.dynamicAllocation.enabled": "true",
                "spark.sql.adaptive.enabled": "true",
                "spark.sql.files.maxPartitionBytes": _max_partition_bytes(i_in_gb),
                "spark.emr-serverless.executor.disk": executor_disk,
                "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
                "spark.sql.shuffle.partitions": str(sp),
                "spark.dynamicAllocation.maxExecutors": str(max_exec),
                "spark.dynamicAllocation.minExecutors": str(min_exec),
                "spark.dynamicAllocation.initialExecutors": str(min_exec),
            }
            cfg.update(_get_timeout_configs(i_in_gb, duration))
            cfg.update(_get_s3_retry_configs())
            cfg.update(_get_iceberg_configs())
            cfg.update(_get_openlineage_configs())
            if sh_ratio > 30:
                cfg.update({"spark.shuffle.compress": "true", "spark.shuffle.spill.compress": "true"})
            # Serverless storage: only when explicitly enabled and disk pressure is safe
            if serverless_storage:
                disk_spill_per_exec = (disk_spill_gb + spill_gb) / max(max_exec, 1)
                if disk_spill_gb == 0 and max_stage_shuf_write <= 150 and disk_spill_per_exec <= 15:
                    cfg["spark.aws.serverlessStorage.enabled"] = "true"
                    cfg.pop("spark.emr-serverless.executor.disk", None)
                    cfg.pop("spark.emr-serverless.executor.disk.type", None)
            return cfg

        # Cost recommendation
        cost_rec = {
            "application_id": app_id,
            "application_name": name,
            "optimization_mode": "cost",
            "metrics": base_metrics,
        }
        if job_id:
            cost_rec["job_id"] = job_id
        cost_rec.update({
            "worker": {
                "type": worker_type,
                "vcpu": worker_cfg["vcpu"],
                "memory_gb": worker_cfg["memory"],
                "max_executors": max_exec_cost,
                "min_executors": min_exec_cost,
                "total_vcpu_capacity": max_exec_cost * worker_cfg["vcpu"],
                "total_memory_capacity": max_exec_cost * worker_cfg["memory"],
            },
            "spark_configs": build_spark_cfg(max_exec_cost, min_exec_cost, sp_cost, executor_disk_cost),
            "shuffle_tuned": {
                "partitions": sp_cost,
                "target_partition_size_mib": target_mib_cost,
                "auto_tuned": True,
            },
        })

        # Performance recommendation
        perf_rec = {
            "application_id": app_id,
            "application_name": name,
            "optimization_mode": "performance",
            "metrics": base_metrics,
        }
        if job_id:
            perf_rec["job_id"] = job_id
        perf_rec.update({
            "worker": {
                "type": worker_type,
                "vcpu": worker_cfg["vcpu"],
                "memory_gb": worker_cfg["memory"],
                "max_executors": max_exec_perf,
                "min_executors": min_exec_perf,
                "total_vcpu_capacity": max_exec_perf * worker_cfg["vcpu"],
                "total_memory_capacity": max_exec_perf * worker_cfg["memory"],
            },
            "spark_configs": build_spark_cfg(max_exec_perf, min_exec_perf, sp_perf, executor_disk_perf),
            "shuffle_tuned": {
                "partitions": sp_perf,
                "target_partition_size_mib": target_mib_perf,
                "auto_tuned": True,
            },
        })

        cost_recs.append(cost_rec)
        perf_recs.append(perf_rec)

        # IO-optimized: only for shuffle I/O bound jobs (>50% time in fetch wait)
        DOWNSIZE_MAP = {
            # (current_type, multiplier) -> target_type
            ("Large", 2): "Medium",
            ("Large", 4): "Small",
            ("Medium", 2): "Small",
        }
        WORKER_RANGES_IO = {
            "Small":  {"vcpu": 4,  "min_mem": 8,  "max_mem": 27, "mem_step": 1},
            "Medium": {"vcpu": 8,  "min_mem": 16, "max_mem": 54, "mem_step": 4},
            "Large":  {"vcpu": 16, "min_mem": 64, "max_mem": 108, "mem_step": 8},
        }
        io_mult = 0
        io_target = None
        if shuffle_fetch_wait_pct > 50:
            # IOPS-based: 5 MB/s effective per disk for shuffle random IO
            # Target: shuffle IO completes within 30% of job duration
            dur_sec = duration * 3600
            total_shuf_mb = (s_in_gb + s_out_gb) * 1024
            target_sec = dur_sec * 0.3
            if target_sec > 0:
                disks_needed = int(total_shuf_mb / (5 * target_sec) + 0.5)
            else:
                disks_needed = max_exec_cost
            # Cap multiplier based on worker count: more workers = more N^2
            # network overhead, so prefer fewer larger workers
            max_allowed = 2 if max_exec_cost > 200 else 4
            io_exec_target = max(max_exec_cost, min(max_exec_cost * max_allowed, disks_needed))
            io_mult = max(1, round(io_exec_target / max_exec_cost)) if max_exec_cost > 0 else 0
            io_mult = min(io_mult, max_allowed)
            io_target = DOWNSIZE_MAP.get((worker_type, io_mult))
            if not io_target and io_mult > 2:
                io_target = DOWNSIZE_MAP.get((worker_type, 4)) or DOWNSIZE_MAP.get((worker_type, 2))
                io_mult = 4 if DOWNSIZE_MAP.get((worker_type, 4)) else 2
            elif not io_target and io_mult > 1:
                io_target = DOWNSIZE_MAP.get((worker_type, 2))
                io_mult = 2
        if io_mult and io_target:
            io_type = io_target
            io_r = WORKER_RANGES_IO[io_type]
            # Keep same per-task memory: original_mem / original_vcpu * new_vcpu
            per_task_mem = worker_cfg["memory"] / worker_cfg["vcpu"]
            io_mem = int(per_task_mem * io_r["vcpu"])
            io_mem = min(io_r["max_mem"], max(io_r["min_mem"],
                         io_r["min_mem"] + (-(-(io_mem - io_r["min_mem"]) // io_r["mem_step"])) * io_r["mem_step"]))
            io_max = max_exec_cost * io_mult
            io_min = min_exec_cost * io_mult
            io_disk = _calculate_executor_disk(s_out_gb, disk_spill_gb, spill_gb, io_max)
            io_cfg = {"vcpu": io_r["vcpu"], "memory": io_mem}
            # Temporarily swap worker_cfg for build_spark_cfg
            saved_cfg, saved_type = worker_cfg, worker_type
            worker_cfg, worker_type = io_cfg, io_type
            io_rec = {
                "application_id": app_id,
                "application_name": name,
                "optimization_mode": "io_optimized",
                "metrics": base_metrics,
            }
            if job_id:
                io_rec["job_id"] = job_id
            io_rec.update({
                "worker": {
                    "type": io_type,
                    "vcpu": io_r["vcpu"],
                    "memory_gb": io_mem,
                    "max_executors": io_max,
                    "min_executors": io_min,
                    "total_vcpu_capacity": io_max * io_r["vcpu"],
                    "total_memory_capacity": io_max * io_mem,
                },
                "spark_configs": build_spark_cfg(io_max, io_min, sp_cost, io_disk),
                "shuffle_tuned": {
                    "partitions": sp_cost,
                    "target_partition_size_mib": target_mib_cost,
                    "auto_tuned": True,
                },
            })
            worker_cfg, worker_type = saved_cfg, saved_type
            # For IO-bound jobs, the IO config IS the cost-efficient config
            cost_recs[-1] = dict(io_rec)
            cost_recs[-1]["optimization_mode"] = "cost"
            # Perf mode: keep large workers if they already have enough disks.
            # Otherwise, find the smallest worker type that meets the disk target
            # while preserving total core count — smaller workers are cheaper.
            if max_exec_perf < io_max:
                perf_orig_cores = max_exec_perf * saved_cfg["vcpu"]
                best = None
                for try_type in ["Small", "Medium", "Large"]:
                    tr = WORKER_RANGES_IO[try_type]
                    need_exec = io_max  # must match cost disk count
                    total_cores = need_exec * tr["vcpu"]
                    if total_cores >= perf_orig_cores:
                        per_task_mem = saved_cfg["memory"] / saved_cfg["vcpu"]
                        tmem = int(per_task_mem * tr["vcpu"])
                        tmem = max(tr["min_mem"], min(tr["max_mem"], tmem))
                        tmem = tmem - (tmem - tr["min_mem"]) % tr["mem_step"] if tmem < tr["max_mem"] else tr["max_mem"]
                        best = (try_type, tr, need_exec, tmem)
                        break
                if best:
                    btype, br, bexec, bmem = best
                    bmin = max(1, bexec // 2)
                    perf_recs[-1]["worker"] = {
                        "type": btype, "vcpu": br["vcpu"], "memory_gb": bmem,
                        "max_executors": bexec, "min_executors": bmin,
                        "total_vcpu_capacity": bexec * br["vcpu"],
                        "total_memory_capacity": bexec * bmem,
                    }
                    perf_recs[-1]["spark_configs"] = build_spark_cfg(
                        bexec, bmin, sp_perf, executor_disk_perf,
                        vcpu_override=br["vcpu"], mem_override=bmem)
                else:
                    # Fallback: inflate original workers to match disk count
                    max_exec_perf = io_max
                    min_exec_perf = max(1, max_exec_perf // 2)
                    perf_recs[-1]["worker"]["max_executors"] = max_exec_perf
                    perf_recs[-1]["worker"]["min_executors"] = min_exec_perf
                    perf_recs[-1]["worker"]["total_vcpu_capacity"] = max_exec_perf * saved_cfg["vcpu"]
                    perf_recs[-1]["worker"]["total_memory_capacity"] = max_exec_perf * saved_cfg["memory"]
                    perf_recs[-1]["spark_configs"] = build_spark_cfg(max_exec_perf, min_exec_perf, sp_perf, executor_disk_perf)
        # No IO rec for non-IO-bound jobs or already-Small workers

    # Process applications with no input data - recommend minimal config
    for _, row in df_no_data.iterrows():
        app_id = row.get('application_id', 'N/A')
        name = row.get('application_name', 'N/A')

        max_exec_minimal = 2
        min_exec_minimal = 1

        minimal_rec = {
            "application_id": app_id,
            "application_name": name,
            "optimization_mode": "minimal",
            "note": "No input data detected - minimal configuration recommended",
            "metrics": {
                "input_gb": 0.0,
                "duration_hours": 0.0,
            },
            "worker": {
                "type": "Small",
                "vcpu": 1,
                "memory_gb": 2,
                "max_executors": max_exec_minimal,
                "min_executors": min_exec_minimal,
                "total_vcpu_capacity": 2,
                "total_memory_capacity": 4
            },
            "spark_configs": {
                "spark.driver.cores": "1",
                "spark.driver.memory": "2G",
                "spark.executor.cores": "1",
                "spark.executor.memory": "2g",
                "spark.executor.instances": str(max_exec_minimal),
                "spark.dynamicAllocation.enabled": "true",
                "spark.dynamicAllocation.maxExecutors": str(max_exec_minimal),
                "spark.dynamicAllocation.minExecutors": str(min_exec_minimal),
                "spark.dynamicAllocation.initialExecutors": str(min_exec_minimal),
                "spark.hadoop.fs.s3a.connection.ssl.enabled": "true",
                "spark.sql.catalog.spark_catalog": "org.apache.iceberg.spark.SparkSessionCatalog",
                "spark.sql.catalog.spark_catalog.type": "hive",
                "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
            }
        }

        cost_recs.append(minimal_rec)
        perf_recs.append(minimal_rec)

    log.info("Generated %d cost, %d performance recommendations",
             len(cost_recs), len(perf_recs))
    return cost_recs, perf_recs


def generate_dual_recommendations_from_data(metrics_data: List[Dict], limit: int = None,
                                             target_partition_size_mib: int = 1024,
                                             serverless_storage: bool = False) -> Tuple[List[Dict], List[Dict]]:
    """
    Generate cost and performance recommendations from metrics data (list of dictionaries).
    This is a wrapper around generate_dual_recommendations that accepts pre-loaded metrics data
    instead of requiring a file path. Used for reading from Iceberg tables.

    Args:
        metrics_data: List of metric dictionaries (same format as load_json_files output)
        limit: Ignored - all applications are processed (kept for backward compatibility)
        target_partition_size_mib: Target shuffle partition size in MiB (default: 1024)
        serverless_storage: Enable serverless storage recommendations

    Returns:
        Tuple of (cost_recommendations, perf_recommendations)
    """

    # Use the same logic as generate_dual_recommendations but with pre-loaded data
    all_data = metrics_data

    # Convert to DataFrame
    flattened = []
    for data in all_data:
        # Handle both old format (io/utilization) and new format (io_summary/executor_summary)
        app_info = data.get('application_info', {})
        io_data = data.get('io', data.get('io_summary', {}).get('application_level', {}))
        util_data = data.get('utilization', data.get('executor_summary', {}))
        spill_data = data.get('spill_summary', {})

        flat = {
            'application_id': data.get('application_id', app_info.get('app_id')),
            'application_name': data.get('application_name', app_info.get('application_name')),
            'job_id': app_info.get('job_id'),
            'total_run_duration_hours': data.get('total_run_duration_hours', app_info.get('total_run_duration_hours')),
            'io_total_input_gb': io_data.get('total_input_gb'),
            'io_total_shuffle_read_gb': io_data.get('total_shuffle_read_gb'),
            'io_total_shuffle_write_gb': io_data.get('total_shuffle_write_gb'),
            'avg_memory_utilization_percent': util_data.get('avg_memory_utilization_percent'),
            'avg_cpu_utilization_percent': util_data.get('avg_cpu_utilization_percent'),
            'idle_core_percentage': util_data.get('idle_core_percentage'),
            'total_memory_spilled_gb': spill_data.get('total_memory_spilled_gb'),
            'total_disk_spilled_gb': spill_data.get('total_disk_spilled_gb'),
            'max_stage_tasks': max((s.get('num_tasks', 0) for s in data.get('stage_summary', {}).get('stages', [])), default=0),
            'max_peak_memory_gb': util_data.get('max_peak_memory_gb', 0),
            'orig_executor_cores': int(data.get('spark_config', {}).get('spark.executor.cores', 0) or 0),
            'orig_total_executors': int(util_data.get('total_executors', 0) or 0),
            'total_task_execution_hours': util_data.get('total_task_execution_hours', 0),
            'max_stage_shuffle_write_gb': data.get('shuffle_data_summary', {}).get('max_stage_shuffle_write_gb', 0),
            'shuffle_fetch_wait_percent': io_data.get('shuffle_fetch_wait_percent', 0),
        }
        flattened.append(flat)

    # Check if we have any data
    if not flattened:
        log.error("No data found in metrics. Please check the input data format.")
        return [], []

    # Debug: Log first record structure
    if flattened and log.isEnabledFor(logging.DEBUG):
        log.debug("Sample flattened record keys: %s", list(flattened[0].keys()))
        log.debug("Sample flattened record: %s", flattened[0])

    df = pd.DataFrame(flattened)

    # Define required columns with defaults
    required_columns = {
        'io_total_input_gb': 0.0,
        'io_total_shuffle_read_gb': 0.0,
        'io_total_shuffle_write_gb': 0.0,
        'avg_memory_utilization_percent': 60.0,
        'avg_cpu_utilization_percent': 50.0,
        'idle_core_percentage': 50.0,
        'total_memory_spilled_gb': 0.0,
        'total_disk_spilled_gb': 0.0,
        'max_stage_tasks': 0,
        'max_peak_memory_gb': 0.0,
        'orig_executor_cores': 0,
        'orig_total_executors': 0,
        'total_task_execution_hours': 0.0,
        'max_stage_shuffle_write_gb': 0.0,
        'shuffle_fetch_wait_percent': 0.0,
        'total_run_duration_hours': 0.0,
    }

    # Ensure all required columns exist
    for col, default_val in required_columns.items():
        if col not in df.columns:
            log.warning("Column '%s' not found in data, using default value: %s", col, default_val)
            df[col] = default_val

    # Sanitize NaN values - replace with 0 for numeric columns
    df = df.fillna(0)

    # Separate apps with and without data (include shuffle-only workloads as "with data")
    has_data = (df['io_total_input_gb'] > 0) | (df['io_total_shuffle_read_gb'] > 0) | (df['io_total_shuffle_write_gb'] > 0)
    df_with_data = df[has_data].sort_values('io_total_input_gb', ascending=False)
    df_no_data = df[~has_data]

    log.info("Processing %d applications with data, %d with no input data", len(df_with_data), len(df_no_data))

    # Call the internal recommendation logic - reuse from generate_dual_recommendations
    # by creating a temporary module context
    import types
    temp_module = types.ModuleType('temp')
    temp_module.log = log
    temp_module._gb_to_gib = _gb_to_gib
    temp_module._calculate_shuffle_ratio = _calculate_shuffle_ratio
    temp_module._select_worker_type = _select_worker_type
    temp_module._compute_exec_limits = _compute_exec_limits
    temp_module._calculate_executor_disk = _calculate_executor_disk
    temp_module._max_partition_bytes = _max_partition_bytes
    temp_module._get_timeout_configs = _get_timeout_configs
    temp_module._get_s3_retry_configs = _get_s3_retry_configs
    temp_module._get_iceberg_configs = _get_iceberg_configs
    temp_module.target_partition_size_mib = target_partition_size_mib
    temp_module.serverless_storage = serverless_storage

    # Reuse the recommendation generation logic by directly calling it
    # We'll inline the core logic here instead of refactoring
    cost_recs = []
    perf_recs = []

    # Process applications with input data
    for _, row in df_with_data.iterrows():
        app_id = row.get('application_id', 'N/A')
        name = row.get('application_name', 'N/A')
        job_id = row.get('job_id')
        duration = float(row.get('total_run_duration_hours', 0) or 0)
        i_in_gb = float(row.get('io_total_input_gb', 0) or 0)
        s_in_gb = float(row.get('io_total_shuffle_read_gb', 0) or 0)
        s_out_gb = float(row.get('io_total_shuffle_write_gb', 0) or 0)

        mem_pct = float(row.get('avg_memory_utilization_percent', 60.0) or 60.0)
        cpu_pct = float(row.get('avg_cpu_utilization_percent', 50.0) or 50.0)
        idle_pct = float(row.get('idle_core_percentage', 50.0) or 50.0)
        spill_gb = float(row.get('total_memory_spilled_gb', 0.0) or 0.0)
        disk_spill_gb = float(row.get('total_disk_spilled_gb', 0.0) or 0.0)
        max_stage_tasks = int(row.get('max_stage_tasks', 0) or 0)
        max_peak_mem_gb = float(row.get('max_peak_memory_gb', 0) or 0)
        orig_cores = int(row.get('orig_executor_cores', 0) or 0)
        orig_executors = int(row.get('orig_total_executors', 0) or 0)
        total_task_exec_hours = float(row.get('total_task_execution_hours', 0) or 0)
        max_stage_shuf_write = float(row.get('max_stage_shuffle_write_gb', 0) or 0)
        shuffle_fetch_wait_pct = float(row.get('shuffle_fetch_wait_percent', 0) or 0)

        sh_ratio = _calculate_shuffle_ratio(i_in_gb, s_in_gb, s_out_gb)
        worker_type, worker_cfg = _select_worker_type(i_in_gb, sh_ratio, mem_pct, spill_gb, cpu_pct,
                                                      max_peak_mem_gb=max_peak_mem_gb, orig_cores=orig_cores)

        shuffle_data_gb = max(s_in_gb, s_out_gb)
        shuffle_bytes = shuffle_data_gb * 1024 * 1024 * 1024
        has_shuffle = shuffle_data_gb > 0

        # Custom partition calculation
        def auto_tune_custom(shuffle_bytes, max_executors):
            target_mib = target_partition_size_mib
            if shuffle_bytes > 0:
                partitions = max(2, int((shuffle_bytes / (target_mib * 1024 * 1024)) + 0.5))
            else:
                # Scale by input size (128MB per partition) instead of flat 200
                partitions = max(2, min(200, int(i_in_gb / 0.128)))
            if partitions % 2 != 0:
                partitions += 1
            return partitions, target_mib

        # Cost-optimized
        max_exec_cost_init, min_exec_cost = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], 0, mem_pct, cpu_pct, idle_pct, spill_gb, mode="cost",
            orig_executors=orig_executors, orig_cores=orig_cores,
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration,
        )
        sp_cost, target_mib_cost = auto_tune_custom(shuffle_bytes, max_exec_cost_init)
        max_exec_cost, min_exec_cost = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], sp_cost, mem_pct, cpu_pct, idle_pct, spill_gb, mode="cost",
            orig_executors=orig_executors, orig_cores=orig_cores,
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration,
        )
        executor_disk_cost = _calculate_executor_disk(s_out_gb, disk_spill_gb, spill_gb, max_exec_cost)

        # Performance-optimized
        max_exec_perf_init, min_exec_perf = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], 0, mem_pct, cpu_pct, idle_pct, spill_gb, mode="performance",
            orig_executors=orig_executors, orig_cores=orig_cores,
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration,
        )
        sp_perf, target_mib_perf = auto_tune_custom(shuffle_bytes, max_exec_perf_init)
        max_exec_perf, min_exec_perf = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], sp_perf, mem_pct, cpu_pct, idle_pct, spill_gb, mode="performance",
            orig_executors=orig_executors, orig_cores=orig_cores,
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration,
        )
        executor_disk_perf = _calculate_executor_disk(s_out_gb, disk_spill_gb, spill_gb, max_exec_perf)

        # Build base metrics
        base_metrics = {
            "input_gb": round(i_in_gb, 2),
            "input_gib": _gb_to_gib(i_in_gb),
            "shuffle_read_gb": round(s_in_gb, 2),
            "shuffle_write_gb": round(s_out_gb, 2),
            "shuffle_total_gb": round(s_in_gb + s_out_gb, 2),
            "shuffle_ratio_percent": sh_ratio,
            "duration_hours": round(duration, 2),
            "avg_memory_utilization_percent": round(mem_pct, 2),
            "avg_cpu_utilization_percent": round(cpu_pct, 2),
            "idle_core_percentage": round(idle_pct, 2),
            "total_memory_spilled_gb": round(spill_gb, 2),
        }

        # Build Spark configs using the same logic as generate_dual_recommendations
        def _driver_sizing(partitions, max_exec, shuffle_gb):
            """Scale driver based on coordination overhead."""
            if partitions > 10000 or max_exec > 500 or shuffle_gb > 10000:
                return 16, 108
            elif partitions > 2000 or max_exec > 100 or shuffle_gb > 2000:
                return 8, 54
            elif partitions > 500 or max_exec > 50 or shuffle_gb > 500:
                return 4, 27
            else:
                return 4, 14

        def build_spark_cfg(max_exec, min_exec, sp, executor_disk, vcpu_override=None, mem_override=None):
            d_cores, d_mem = _driver_sizing(sp, max_exec, s_in_gb + s_out_gb)
            vcpu = vcpu_override or worker_cfg["vcpu"]
            mem = mem_override or worker_cfg["memory"]
            cfg = {
                "spark.driver.cores": str(d_cores),
                "spark.driver.memory": f"{d_mem}G",
                "spark.executor.cores": str(vcpu),
                "spark.executor.memory": f"{mem}g",
                "spark.dynamicAllocation.enabled": "true",
                "spark.sql.adaptive.enabled": "true",
                "spark.sql.files.maxPartitionBytes": _max_partition_bytes(i_in_gb),
                "spark.emr-serverless.executor.disk": executor_disk,
                "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
                "spark.sql.shuffle.partitions": str(sp),
                "spark.dynamicAllocation.maxExecutors": str(max_exec),
                "spark.dynamicAllocation.minExecutors": str(min_exec),
                "spark.dynamicAllocation.initialExecutors": str(min_exec),
            }
            cfg.update(_get_timeout_configs(i_in_gb, duration))
            cfg.update(_get_s3_retry_configs())
            cfg.update(_get_iceberg_configs())
            cfg.update(_get_openlineage_configs())
            if sh_ratio > 30:
                cfg.update({"spark.shuffle.compress": "true", "spark.shuffle.spill.compress": "true"})
            # Serverless storage: only when explicitly enabled and disk pressure is safe
            if serverless_storage:
                disk_spill_per_exec = (disk_spill_gb + spill_gb) / max(max_exec, 1)
                if disk_spill_gb == 0 and max_stage_shuf_write <= 150 and disk_spill_per_exec <= 15:
                    cfg["spark.aws.serverlessStorage.enabled"] = "true"
                    cfg.pop("spark.emr-serverless.executor.disk", None)
                    cfg.pop("spark.emr-serverless.executor.disk.type", None)
            return cfg

        # Cost-optimized recommendation
        rec_cost = {
            "application_id": app_id,
            "application_name": name,
            "job_id": job_id,
            "optimization_mode": "cost",
            "metrics": base_metrics,
            "worker": {
                "type": worker_type,
                "vcpu": worker_cfg["vcpu"],
                "memory_gb": worker_cfg["memory"],
                "max_executors": max_exec_cost,
                "min_executors": min_exec_cost,
                "total_vcpu_capacity": max_exec_cost * worker_cfg["vcpu"],
            },
            "configuration": {
                "shuffle_partitions": sp_cost,
                "executor_disk": executor_disk_cost,
            },
            "spark_config": build_spark_cfg(max_exec_cost, min_exec_cost, sp_cost, executor_disk_cost),
        }
        cost_recs.append(rec_cost)

        # Performance-optimized recommendation
        rec_perf = {
            "application_id": app_id,
            "application_name": name,
            "job_id": job_id,
            "optimization_mode": "performance",
            "metrics": base_metrics,
            "worker": {
                "type": worker_type,
                "vcpu": worker_cfg["vcpu"],
                "memory_gb": worker_cfg["memory"],
                "max_executors": max_exec_perf,
                "min_executors": min_exec_perf,
                "total_vcpu_capacity": max_exec_perf * worker_cfg["vcpu"],
            },
            "configuration": {
                "shuffle_partitions": sp_perf,
                "executor_disk": executor_disk_perf,
            },
            "spark_config": build_spark_cfg(max_exec_perf, min_exec_perf, sp_perf, executor_disk_perf),
        }
        perf_recs.append(rec_perf)

    # Process apps with no data
    for _, row in df_no_data.iterrows():
        app_id = row.get('application_id', 'N/A')
        name = row.get('application_name', 'N/A')
        job_id = row.get('job_id')

        minimal_rec = {
            "application_id": app_id,
            "application_name": name,
            "job_id": job_id,
            "optimization_mode": "default",
            "metrics": {
                "input_gb": 0,
                "shuffle_read_gb": 0,
                "shuffle_write_gb": 0,
                "shuffle_total_gb": 0,
                "shuffle_ratio_percent": 0.0,
                "duration_hours": 0,
                "avg_memory_utilization_percent": 0,
                "avg_cpu_utilization_percent": 0,
                "idle_core_percentage": 0,
                "total_memory_spilled_gb": 0,
            },
            "worker": {
                "type": "G.2X",
                "vcpu": 8,
                "memory_gb": 32,
                "max_executors": 10,
                "min_executors": 1,
                "total_vcpu_capacity": 80,
            },
            "configuration": {
                "shuffle_partitions": 200,
                "executor_disk": "20GB",
            },
            "spark_config": {
                "spark.driver.cores": "4",
                "spark.driver.memory": "14G",
                "spark.executor.cores": "8",
                "spark.executor.memory": "32g",
                "spark.dynamicAllocation.enabled": "true",
                "spark.dynamicAllocation.maxExecutors": "10",
                "spark.dynamicAllocation.minExecutors": "1",
                "spark.dynamicAllocation.initialExecutors": "1",
                "spark.sql.shuffle.partitions": "200",
            }
        }

        cost_recs.append(minimal_rec)
        perf_recs.append(minimal_rec)

    log.info("Generated %d cost, %d performance recommendations from metrics data",
             len(cost_recs), len(perf_recs))
    return cost_recs, perf_recs


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Dual-mode EMR Serverless recommender (S3 or Local)")
    parser.add_argument("--input-path", required=True, help="S3 path (s3://bucket/prefix) or local path")
    parser.add_argument("--region", default="us-east-1", help="AWS region (for S3 only)")
    parser.add_argument("--limit", type=int, default=None, help="(Deprecated) Max applications - no longer enforced, all apps are processed")
    parser.add_argument("--target-partition-size", type=int, default=1024,
                        help="Target shuffle partition size in MiB (default: 1024 = 1GB)")
    parser.add_argument("--output-cost", default="recommendations_cost_optimized.json", help="Cost output file")
    parser.add_argument("--output-perf", default="recommendations_performance_optimized.json", help="Performance output file")
    parser.add_argument("--format-job-config", action="store_true",
                        help="Format output to job configuration format")
    parser.add_argument("--cost-optimized", action="store_true",
                        help="Generate only cost-optimized recommendations")
    parser.add_argument("--performance-optimized", action="store_true",
                        help="Generate only performance-optimized recommendations")
    parser.add_argument("--individual-files", action="store_true",
                        help="Generate individual JSON files per job (1-jobname.json, 2-jobname.json, ...)")
    parser.add_argument("--serverless-storage", action="store_true",
                        help="Enable serverless storage recommendations (disabled by default)")
    parser.add_argument("--write-to-iceberg-table",
                        help="Write recommendations to Iceberg table (catalog.database.table)")

    args = parser.parse_args()

    # Generate recommendations
    cost_recs, perf_recs = generate_dual_recommendations(
        args.input_path,
        args.limit,
        args.target_partition_size,
        serverless_storage=args.serverless_storage
    )

    # Check if we have any recommendations
    if not cost_recs and not perf_recs:
        log.warning("No recommendations generated - input data may be empty or have no applications with metrics")
        # Write empty recommendation files to avoid downstream errors
        with open(args.output_cost, 'w') as f:
            json.dump([], f)
        log.info(f"Empty recommendations written to {args.output_cost}")
        if args.output_perf and args.output_perf != args.output_cost:
            with open(args.output_perf, 'w') as f:
                json.dump([], f)
            log.info(f"Empty recommendations written to {args.output_perf}")
        sys.exit(0)

    # Determine which recommendations to generate
    generate_cost = not args.performance_optimized  # Generate cost unless perf-only
    generate_perf = not args.cost_optimized  # Generate perf unless cost-only

    # Write recommendations
    if generate_cost:
        if args.format_job_config:
            from format_to_job_config import format_to_job_config
            cost_jobs = [format_to_job_config(rec) for rec in cost_recs]

            if args.individual_files:
                # Write individual files
                output_dir = Path(args.output_cost).parent
                output_dir.mkdir(parents=True, exist_ok=True)
                for i, job in enumerate(cost_jobs, 1):
                    job_name = job.get('job_name', f'job_{i}').replace(' ', '_').replace('-job', '')
                    filename = output_dir / f"{i}-{job_name}.json"
                    filename.write_text(json.dumps(job, indent=2))
                log.info("Cost-optimized job configs written to %d individual files in %s", len(cost_jobs), output_dir)
            else:
                # Write single file
                cost_job_file = args.output_cost.replace('.json', '_job_config.json')
                Path(cost_job_file).parent.mkdir(parents=True, exist_ok=True)
                Path(cost_job_file).write_text(json.dumps(cost_jobs, indent=2))
                log.info("Cost-optimized job config written to %s", cost_job_file)
        else:
            Path(args.output_cost).write_text(json.dumps(cost_recs, indent=2))
            log.info("Cost-optimized recommendations written to %s", args.output_cost)

    if generate_perf:
        if args.format_job_config:
            from format_to_job_config import format_to_job_config
            perf_jobs = [format_to_job_config(rec) for rec in perf_recs]

            if args.individual_files:
                # Write individual files
                output_dir = Path(args.output_perf).parent
                output_dir.mkdir(parents=True, exist_ok=True)
                for i, job in enumerate(perf_jobs, 1):
                    job_name = job.get('job_name', f'job_{i}').replace(' ', '_').replace('-job', '')
                    filename = output_dir / f"{i}-{job_name}.json"
                    filename.write_text(json.dumps(job, indent=2))
                log.info("Performance-optimized job configs written to %d individual files", len(perf_jobs))
            else:
                # Write single file
                perf_job_file = args.output_perf.replace('.json', '_job_config.json')
                Path(perf_job_file).parent.mkdir(parents=True, exist_ok=True)
                Path(perf_job_file).write_text(json.dumps(perf_jobs, indent=2))
                log.info("Performance-optimized job config written to %s", perf_job_file)
        else:
            Path(args.output_perf).write_text(json.dumps(perf_recs, indent=2))
            log.info("Performance-optimized recommendations written to %s", args.output_perf)

    # Write to Iceberg table if requested
    if args.write_to_iceberg_table:
        from write_to_iceberg import write_to_iceberg
        recs_to_write = []
        if generate_cost:
            recs_to_write.extend(cost_recs)
        if generate_perf:
            recs_to_write.extend(perf_recs)
        write_to_iceberg(recs_to_write, args.write_to_iceberg_table, args.region)

    # Print comparison (only if both modes generated)
    if generate_cost and generate_perf:
        print("\n" + "="*80)
        print("COMPARISON SUMMARY")
        print("="*80)
    print(f"{'App Name':<40} | {'Mode':<11} | {'Max Exec':>8} | {'Total vCPU':>10}")
    print("-"*80)
    for cost, perf in zip(cost_recs[:10], perf_recs[:10]):
        name = cost['application_name'][:38]
        print(f"{name:<40} | {'Cost':<11} | {cost['worker']['max_executors']:>8} | {cost['worker']['total_vcpu_capacity']:>10}")
        print(f"{'':<40} | {'Performance':<11} | {perf['worker']['max_executors']:>8} | {perf['worker']['total_vcpu_capacity']:>10}")
        diff_exec = perf['worker']['max_executors'] - cost['worker']['max_executors']
        diff_pct = (diff_exec / cost['worker']['max_executors'] * 100) if cost['worker']['max_executors'] > 0 else 0
        print(f"{'':<40} | {'Difference':<11} | {diff_exec:>+8} | {diff_pct:>+9.1f}%")
        print("-"*80)