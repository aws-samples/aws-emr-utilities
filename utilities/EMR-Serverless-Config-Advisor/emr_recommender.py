#!/usr/bin/env python3
"""
EMR Serverless Recommender - Dual Mode with Local/S3 Support
Supports both local filesystem and S3 for input/output.
"""

import sys
import math
import json
import glob
from pathlib import Path
from typing import List, Dict, Tuple
import pandas as pd
import logging
import re

def _parse_size_to_bytes(val):
    """Parse size strings like '256m', '256MB', '1g', '1024' to bytes."""
    if not val or str(val) in ('', 'None'):
        return 0
    s = str(val).strip()
    m = re.match(r'^(\d+)\s*([a-zA-Z]*)$', s)
    if not m:
        return 0
    num = int(m.group(1))
    unit = m.group(2).upper()
    if unit in ('M', 'MB'):
        return num * 1024 * 1024
    elif unit in ('G', 'GB'):
        return num * 1024 * 1024 * 1024
    elif unit in ('K', 'KB'):
        return num * 1024
    return num

def _parse_size_to_mb(val):
    """Parse size string to megabytes."""
    b = _parse_size_to_bytes(val)
    return b // (1024 * 1024) if b else 0

# Setup logging
logging.basicConfig(
    format="%(asctime)s %(levelname)-5s [%(name)s]  %(message)s",
    level=logging.INFO,
)
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


def load_json_files(path: str, limit: int = 100) -> List[Dict]:
    """Load JSON files from S3 or local filesystem."""
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
                    
                    if len(all_data) >= limit:
                        break
                except Exception as e:
                    log.debug("Skipping %s: %s", key, e)
            
            if len(all_data) >= limit:
                break
    else:
        # Local filesystem
        search_path = Path(path) / 'task_stage_summary' / '*.json'
        log.info("Loading from local: %s", search_path)
        
        json_files = glob.glob(str(search_path))
        for json_file in json_files[:limit]:
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


def _is_ec2_source(spark_config: Dict) -> bool:
    """Detect if event log is from EMR on EC2 (vs EMR Serverless)."""
    return bool(spark_config.get('spark.emr_cluster_id', ''))


def _compute_broadcast_threshold(executor_memory_gb: int, max_executors: int) -> str:
    """Compute optimal broadcast threshold for Serverless target.
    Balances memory budget (10% of executor mem / 3 concurrent broadcasts)
    against network cost (total broadcast traffic < 50GB).
    Capped at 256MB to prevent OOM from concurrent broadcasts.
    """
    mem_cap_mb = int(executor_memory_gb * 1024 * 0.10 / 3)
    network_cap_mb = int(50 * 1024 / max(max_executors, 1))
    threshold_mb = min(mem_cap_mb, network_cap_mb, 256)
    return f"{threshold_mb}MB"


def _select_worker_type(input_gb: float, shuffle_ratio: float,
                        mem_pct: float = 60.0, spill_gb: float = 0.0,
                        cpu_pct: float = 50.0, orig_mem_mb: int = 0,
                        max_peak_mem_gb: float = 0, orig_cores: int = 0,
                        max_shuffle_write_per_task_gb: float = 0,
                        peak_mem_pct: float = 0, is_ec2: bool = False) -> Tuple[str, Dict]:
    # EMR Serverless memory ranges per vCPU size
    WORKER_RANGES = {
        "Small":  {"vcpu": 4,  "min_mem": 8,  "max_mem": 27, "mem_step": 1},
        "Medium": {"vcpu": 8,  "min_mem": 16, "max_mem": 54, "mem_step": 4},
        "Large":  {"vcpu": 16, "min_mem": 64, "max_mem": 108, "mem_step": 8},
    }

    # Worker selection for EC2→Serverless migration:
    # Balance memory fit + shuffle coordination (fewer executors = less N² overhead)
    if is_ec2:
        # Worker selection based on workload profile:
        # 8c: shuffle/spill-heavy jobs benefit from higher per-executor disk throughput (250 MiB/s)
        # 4c: input-heavy jobs benefit from more executors (more S3 read parallelism)
        orig_mem_gb = orig_mem_mb / 1024 if orig_mem_mb > 0 else 20
        mem_per_core = orig_mem_gb * mem_pct / 100 / max(orig_cores, 1)
        mem_for_medium = max(20, mem_per_core * (8 ** 0.7) * 1.3)

        if mem_for_medium > 54:
            size = "Large"
        elif max_shuffle_write_per_task_gb >= 1.0 or spill_gb > 20000:
            # High per-task shuffle write needs Medium for memory headroom
            size = "Medium"
        else:
            # Default to Small (4c) for S3 read parallelism
            # Worker bump logic later promotes to Medium when executor count > 60
            size = "Small"
    else:
        if spill_gb > 100:
            size = "Large"
        elif spill_gb > 10 or mem_pct > 90:
            size = "Medium"
        else:
            size = "Small"

    r = WORKER_RANGES[size]

    # Memory right-sizing: use observed peak memory to pick appropriate memory level
    # instead of always maxing out (which over-provisions and slows worker allocation)
    if size == "Small":
        # For Small workers: right-size based on observed peak memory
        # 2x headroom for stability (prevents OOM under data growth)
        if max_peak_mem_gb > 0 and peak_mem_pct > 0:
            needed_mem = max_peak_mem_gb * 2.0  # 2x headroom for OOM safety
            needed_mem = max(needed_mem, 8)  # minimum 8g
            mem = min(r["max_mem"], max(r["min_mem"], int(needed_mem + 0.99)))
        elif mem_pct > 0 and orig_mem_mb > 0:
            used_gb = (orig_mem_mb / 1024) * (mem_pct / 100)
            needed_mem = used_gb * 2.0
            mem = min(r["max_mem"], max(r["min_mem"], int(needed_mem + 0.99)))
        else:
            # No data — use max (safe default)
            mem = r["max_mem"]
    elif size == "Medium":
        if max_peak_mem_gb > 0:
            needed_mem = max_peak_mem_gb * 2.0
            mem = min(r["max_mem"], max(r["min_mem"], int(needed_mem / r["mem_step"] + 0.99) * r["mem_step"]))
        else:
            mem = r["max_mem"]
    else:
        mem = r["max_mem"]

    return size, {"vcpu": r["vcpu"], "memory": mem}


def _compute_exec_limits(input_gb: float, vcpu: int, partitions: int = 0,
                        mem_pct: float = 60.0, cpu_pct: float = 50.0,
                        idle_pct: float = 50.0, spill_gb: float = 0.0,
                        mode: str = "cost",
                        orig_executors: int = 0, orig_cores: int = 0,
                        orig_mem_mb: int = 0,
                        total_task_exec_hours: float = 0,
                        duration_hours: float = 0,
                        stages: list = None,
                        is_ec2_source: bool = False) -> Tuple[int, int]:
    # Work-based metrics
    work = total_task_exec_hours / duration_hours if duration_hours > 0 else 0
    orig_vcpu = orig_executors * orig_cores if orig_executors > 0 and orig_cores > 0 else 0
    eff = total_task_exec_hours / (orig_vcpu * duration_hours) if orig_vcpu > 0 and duration_hours > 0 else 0.5

    # Compute shuffle ratio from stages
    total_shuf_write = sum(s.get('shuffle_write_gb', 0) for s in (stages or []))
    total_input = sum(s.get('input_gb', 0) for s in (stages or []))
    shuf_ratio = total_shuf_write / max(total_input, 1) if total_input > 0 else (total_shuf_write / max(input_gb, 1))

    if is_ec2_source:
        # --- EC2 → Serverless: apply efficiency gain ---
        # Serverless 8c/54G executors are more efficient than EC2 executors due to:
        # 1. More memory per core → less spill, less GC
        # 2. Larger executors → less shuffle overhead
        # The efficiency gain depends on how much MORE memory/core Serverless provides.
        serverless_mem_per_core = 54 / 8 if vcpu == 8 else 27 / 4  # target worker mem/core
        ec2_mem_per_core = (orig_mem_mb / 1024) / orig_cores if orig_cores > 0 and orig_mem_mb > 0 else 6.0

        # Memory efficiency: if EC2 had less mem/core, Serverless gains more (less spill)
        # If EC2 already had equal or more mem/core, gain is minimal
        mem_ratio = min(1.0, ec2_mem_per_core / serverless_mem_per_core)
        # Scaling: ranges from 0.47 (EC2 had low mem/core) to 0.80 (EC2 had high mem/core)
        base_efficiency = 0.47 + 0.33 * mem_ratio
        per_exec_factor = min(1.4, base_efficiency * orig_cores) if orig_cores > 0 else 1.4
        base_cores = per_exec_factor * orig_executors + 65 if orig_executors > 0 else max(50, int(input_gb / 10))

        # Rule 1: Shuffle boost for small clusters with very high shuffle ratio
        if orig_executors < 110 and shuf_ratio > 10:
            cores = base_cores + total_shuf_write / 30
        # Rule 2: Cap for large over-provisioned clusters
        elif orig_executors > 150 and shuf_ratio < 8 and eff < 0.40:
            mult = max(0.5, 1.8 - eff * 3)
            io_boost = input_gb / 5 if shuf_ratio < 1 else 0
            cores = work * mult + io_boost + 30
            # Sub-rule: high spill needs more parallelism for I/O throughput
            if spill_gb > 5000:
                cores = cores * 1.3
        else:
            cores = base_cores
    else:
        # --- Serverless source: use actual configuration directly ---
        # The source already ran on Serverless — match its configured capacity.
        # orig_vcpu = what was set as maxExecutors × cores (the proven config).
        # Add 10% headroom for variability between runs.
        cores = max(work * 1.1, orig_vcpu * 1.1) if orig_vcpu > 0 else work * 1.1

    max_exec = max(4, int(cores / vcpu))  # EMR Serverless minimum is 4

    # Performance mode: 1.5x for faster completion
    if mode == "performance":
        max_exec = int(max_exec * 1.5)

    # minExecutors: pre-warm 1/3 of max for fast ramp-up on short queries
    # Confirmed: minExec=3 causes 2x regression on short queries due to slow dynamic allocation ramp-up
    min_exec = max(3, max_exec // 3)
    initial_exec = min(max_exec, max(5, max_exec // 4))  # 25% of max, floor 5

    return max_exec, min_exec


def _calculate_executor_disk(shuffle_write_gb: float, disk_spill_gb: float,
                             memory_spill_gb: float, max_executors: int) -> str:
    """Attach shuffle_optimized disk for shuffle-intensive jobs.
    Shuffle-optimized disks provide higher IOPS and faster disk access,
    benefiting jobs with significant shuffle operations or disk spill.
    For non-shuffle workloads, default disk avoids unnecessary cost.
    """
    total_shuffle_and_spill = shuffle_write_gb + disk_spill_gb + memory_spill_gb
    if total_shuffle_and_spill < 20:
        return ""  # Minimal shuffle — default disk sufficient
    # Shuffle-intensive: attach shuffle_optimized disk
    # Minimum 500G — empirically proven that 200G has 1.5-1.8x slower I/O
    # due to linear throughput scaling on shuffle_optimized volumes
    per_exec = total_shuffle_and_spill / max(max_executors, 1)
    disk_gb = max(200, min(2000, int(per_exec * 1.5 / 20) * 20 + 20))
    return f"{disk_gb}G"


def _max_partition_bytes(input_gb: float, advisory_bytes: int = 0) -> str:
    """Compute maxPartitionBytes for scan stages.
    
    Controls how much data each scan task reads. Should be large enough
    to avoid excessive task overhead but small enough to fit in memory.
    For shuffle-heavy jobs, scan partition size matters less (AQE reshuffles).
    """
    if advisory_bytes >= 500_000_000:  # 500MB+ advisory — large partitions OK
        return "512m"
    elif advisory_bytes >= 256_000_000:
        return "256m"
    elif advisory_bytes > 0:
        # Align scan partitions with advisory to reduce coalescing overhead
        return "128m"
    # No advisory: scale with input size
    if input_gb >= 2000:
        return "512m"
    elif input_gb >= 500:
        return "256m"
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


def _get_s3_retry_configs(input_gb: float, output_gb: float = 0) -> Dict[str, str]:
    if input_gb < 100:
        retries = "5"
    elif input_gb < 1000:
        retries = "10"
    else:
        retries = "15"
    
    s3_io_gb = input_gb + output_gb
    if s3_io_gb > 1000:
        attempts_maximum = "15"
    elif s3_io_gb > 100:
        attempts_maximum = "10"
    else:
        attempts_maximum = None  # use Hadoop default (5)
    
    configs = {
        "spark.hadoop.fs.s3a.retry.limit": retries,
        "spark.hadoop.fs.s3a.connection.ssl.enabled": "true",
    }
    if attempts_maximum:
        configs["spark.hadoop.fs.s3a.attempts.maximum"] = attempts_maximum
    return configs


def _get_iceberg_configs() -> Dict[str, str]:
    return {
        "spark.sql.catalog.spark_catalog": "org.apache.iceberg.spark.SparkSessionCatalog",
        "spark.sql.catalog.spark_catalog.type": "hive",
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
    }



def _detect_window_group_limit_skew(stages, duration_min):
    """Detect WindowGroupLimit-induced skew: few tasks, long duration, high spill/skew."""
    findings = []
    for s in stages:
        num_tasks = s.get('num_tasks', 0)
        duration_sec = s.get('duration_sec', 0) or 0
        total_task_time = s.get('total_task_time_sec', 0) or 0
        mem_spill_gb = s.get('mem_spill_gb', 0) or 0
        if num_tasks == 0 or duration_sec == 0 or total_task_time == 0:
            continue
        avg_task_time = total_task_time / num_tasks
        skew_ratio = duration_sec / avg_task_time if avg_task_time > 0 else 0
        is_few_tasks = num_tasks < 50
        is_long = duration_sec > 1800
        is_skewed = skew_ratio > 5.0
        has_spill = mem_spill_gb > 100
        if is_few_tasks and is_long and (is_skewed or has_spill):
            pct_of_job = (duration_sec / 60) / duration_min * 100 if duration_min > 0 else 0
            findings.append({
                'stage_id': s.get('stage_id'),
                'num_tasks': num_tasks,
                'duration_min': round(duration_sec / 60, 1),
                'skew_ratio': round(skew_ratio, 1),
                'mem_spill_gb': round(mem_spill_gb, 1),
                'pct_of_job': round(pct_of_job, 1),
            })
    return findings



def _detect_window_group_limit_coalesce_regression(stages, sql_executions, executor_summary):
    """Detect WindowGroupLimit regression from AQE coalescing imbalance."""
    has_window = any('Window ' in (sq.get('physical_plan_description', '') or '') or
                     'Window[' in (sq.get('physical_plan_description', '') or '')
                     for sq in sql_executions)
    if not has_window:
        return False
    small_coalesced = [s for s in stages if 1 < s.get('num_tasks', 0) < 100]
    if len(small_coalesced) <= 10:
        return False
    fetch_failed_large = [s for s in stages
                          if s.get('failure_reason')
                          and 'FetchFailed' in (s.get('failure_reason') or '')
                          and s.get('num_tasks', 0) > 500]
    if not fetch_failed_large:
        return False
    nearby = sum(1 for sm in small_coalesced for fl in fetch_failed_large
                 if abs(sm['stage_id'] - fl['stage_id']) <= 30)
    if nearby <= 5:
        return False
    dead = executor_summary.get('dead_executors', 0)
    total = executor_summary.get('total_executors', 1)
    if total > 0 and dead / total >= 0.10:
        return False
    return True

def generate_dual_recommendations(input_path: str, limit: int = 100,
                                  target_partition_size_mib: int = 1024,
                                  serverless_storage: bool = False) -> Tuple[List[Dict], List[Dict]]:
    """Generate cost, performance, and IO-optimized recommendations."""
    
    # Load metrics
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
            'io_total_output_gb': io_data.get('total_output_gb', 0),
            'io_total_shuffle_read_gb': io_data.get('total_shuffle_read_gb'),
            'io_total_shuffle_write_gb': io_data.get('total_shuffle_write_gb'),
            'avg_memory_utilization_percent': util_data.get('avg_memory_utilization_percent'),
            'avg_cpu_utilization_percent': util_data.get('avg_cpu_utilization_percent'),
            'idle_core_percentage': util_data.get('idle_core_percentage'),
            'total_memory_spilled_gb': spill_data.get('total_memory_spilled_gb'),
            'total_disk_spilled_gb': spill_data.get('total_disk_spilled_gb'),
            'max_stage_tasks': max((s.get('num_tasks', 0) for s in data.get('stage_summary', {}).get('stages', [])), default=0),
            'total_tasks': data.get('task_summary', {}).get('total_tasks', 0),
            'max_peak_memory_gb': util_data.get('max_peak_memory_gb', 0),
            'orig_executor_cores': int(data.get('spark_config', {}).get('spark.executor.cores', 0) or 0),
            'orig_executor_mem_gb': float(''.join(c for c in str(data.get('spark_config', {}).get('spark.executor.memory', '0g')).lower().replace('g','').replace('m','') if c.isdigit() or c == '.') or 0),
            'orig_executor_mem_gb': float(''.join(c for c in str(data.get('spark_config', {}).get('spark.executor.memory', '0g')).replace('G','g').replace('m','') if c.isdigit() or c == '.') or 0),
            'orig_total_executors': int(util_data.get('total_executors', 0) or 0),
            'total_task_execution_hours': util_data.get('total_task_execution_hours', 0),
            'max_stage_shuffle_write_gb': data.get('shuffle_data_summary', {}).get('max_stage_shuffle_write_gb', 0),
            'shuffle_fetch_wait_percent': io_data.get('shuffle_fetch_wait_percent', 0),
            '_stages_raw': data.get('stage_summary', {}).get('stages', []),
            '_sql_executions_raw': data.get('sql_executions', []),
            '_executor_summary_raw': data.get('executor_summary', {}),
            '_spark_config_raw': data.get('spark_config', {}),
        }
        flattened.append(flat)
    
    df = pd.DataFrame(flattened)
    
    # Sanitize NaN values - replace with 0 for numeric columns
    df = df.fillna(0)
    
    # Separate apps with and without data (include shuffle-only workloads as "with data")
    has_data = (df['io_total_input_gb'] > 0) | (df['io_total_shuffle_read_gb'] > 0) | (df['io_total_shuffle_write_gb'] > 0)
    df_with_data = df[has_data].sort_values('io_total_input_gb', ascending=False).head(limit)
    df_no_data = df[~has_data].head(limit)
    
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
        i_out_gb = float(row.get('io_total_output_gb', 0) or 0)
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
        orig_executor_mem_gb = float(row.get('orig_executor_mem_gb', 0) or 0)
        orig_executor_mem_gb = float(row.get('orig_executor_mem_gb', 0) or 0)
        orig_executors = int(row.get('orig_total_executors', 0) or 0)
        total_task_exec_hours = float(row.get('total_task_execution_hours', 0) or 0)
        max_stage_shuf_write = float(row.get('max_stage_shuffle_write_gb', 0) or 0)
        shuffle_fetch_wait_pct = float(row.get('shuffle_fetch_wait_percent', 0) or 0)
        
        # Source detection and per-task metrics for worker sizing
        _spark_config_raw = row.get('_spark_config_raw', {})
        _executor_summary_raw = row.get('_executor_summary_raw', {})
        is_ec2 = _is_ec2_source(_spark_config_raw)
        peak_mem_pct = float(_executor_summary_raw.get('max_memory_utilization_percent', 0) or 0)
        stages_raw = row.get('_stages_raw', [])
        max_shuffle_write_per_task_gb = max(
            (s.get('shuffle_write_gb', 0) / s['num_tasks']
             for s in stages_raw if s.get('num_tasks', 0) > 0),
            default=0
        )

        sh_ratio = _calculate_shuffle_ratio(i_in_gb, s_in_gb, s_out_gb)
        worker_type, worker_cfg = _select_worker_type(i_in_gb, sh_ratio, mem_pct, spill_gb, cpu_pct,
                                                      max_peak_mem_gb=max_peak_mem_gb, orig_cores=orig_cores, orig_mem_mb=int(orig_executor_mem_gb * 1024),
                                                      max_shuffle_write_per_task_gb=max_shuffle_write_per_task_gb,
                                                      peak_mem_pct=peak_mem_pct, is_ec2=is_ec2)

        # Large advisory (>=500MB) means large partitions — need 8c for memory headroom
        _src_adv = str(_spark_config_raw.get('spark.sql.adaptive.advisoryPartitionSizeInBytes', ''))
        if is_ec2 and worker_type == "Small" and 'MB' in _src_adv.upper():
            _adv_val = _parse_size_to_mb(_src_adv)
            if _adv_val >= 500:
                worker_type = "Medium"
                worker_cfg = {"vcpu": 8, "memory": 54}

        # Extreme spill+shuffle from large EC2 cluster: use Large (16c/108G) for max throughput
        if is_ec2 and orig_executors > 200 and spill_gb > 25000 and disk_spill_gb > 1000:
            worker_type = "Large"
            worker_cfg = {"vcpu": 16, "memory": 108}

        # If source uses broadcast > 256MB, upsize worker to safely hold broadcast tables
        _src_bc = str(_spark_config_raw.get('spark.sql.autoBroadcastJoinThreshold', ''))
        if _src_bc and _src_bc not in ('-1', 'None', ''):
            _bc_mb = 0
            if _src_bc.upper().endswith('MB'):
                _bc_mb = int(''.join(c for c in _src_bc[:-2] if c.isdigit()) or 0)
            elif _src_bc.lower().endswith('m'):
                _bc_mb = int(''.join(c for c in _src_bc[:-1] if c.isdigit()) or 0)
            if _bc_mb > 256 and worker_type == "Small":
                worker_type = "Medium"
                worker_cfg = {"vcpu": 8, "memory": 54}

        shuffle_data_gb = max(s_in_gb, s_out_gb)
        shuffle_bytes = shuffle_data_gb * 1024 * 1024 * 1024
        has_shuffle = shuffle_data_gb > 0
        
        # Custom partition calculation
        def auto_tune_custom(shuffle_bytes, max_executors):
            # Adjust target partition size based on memory utilization
            # High memory pressure -> smaller partitions to reduce per-task memory
            if mem_pct > 90:
                target_mib = min(target_partition_size_mib, 128)
            elif mem_pct > 85:
                target_mib = min(target_partition_size_mib, 256)
            else:
                target_mib = target_partition_size_mib
            if shuffle_bytes > 0:
                partitions = max(2, int((shuffle_bytes / (target_mib * 1024 * 1024)) + 0.5))
            else:
                # Scale by input size (128MB per partition) instead of flat 200
                partitions = max(2, min(200, int(i_in_gb / 0.128)))
            if partitions % 2 != 0:
                partitions += 1
            return partitions, target_mib

        def cap_partitions(partitions, max_executors):
            """Cap partitions based on executor IO concurrency, with a
            data volume floor to prevent oversized partitions."""
            io_ceiling = max(200, max_executors * 8)
            # Data floor: ensure partitions don't exceed 3x memory per core
            mem_per_core = worker_cfg["memory"] / worker_cfg["vcpu"]
            max_gb_per_part = mem_per_core * 3
            data_floor = max(2, int((s_in_gb + s_out_gb) / max_gb_per_part)) if max_gb_per_part > 0 else 200
            # Spill override: if spill is significant relative to shuffle/input,
            # partitions are undersized — don't cap them down
            total_data_gb = max(s_in_gb + s_out_gb, 1)
            spill_ratio = spill_gb / total_data_gb
            if spill_ratio > 0.5:  # spill > 50% of data volume
                # Preserve higher partitions but cap at total vCPU capacity
                spill_ceiling = max(io_ceiling, max_executors * worker_cfg["vcpu"])
                partitions = max(data_floor, min(partitions, spill_ceiling))
            elif mem_pct > 85:  # high memory pressure
                # Allow more partitions to reduce per-task memory, cap at total vCPU
                mem_ceiling = max(io_ceiling, max_executors * worker_cfg["vcpu"])
                partitions = max(data_floor, min(partitions, mem_ceiling))
            elif mem_pct < 70 and idle_pct > 50:  # over-provisioned, low memory pressure
                # Fewer partitions reduces scheduling overhead, cap at half total vCPU
                low_mem_ceiling = max(io_ceiling, max_executors * worker_cfg["vcpu"] // 2)
                partitions = max(data_floor, min(partitions, low_mem_ceiling))
            else:
                # Apply: at least the data floor, at most the IO ceiling
                partitions = max(data_floor, min(partitions, io_ceiling))
            if partitions % 2 != 0:
                partitions += 1
            return partitions
        
        # --- WindowGroupLimit skew detection ---
        duration_min_raw = duration * 60
        window_skew_findings = _detect_window_group_limit_skew(stages_raw, duration_min_raw)
        window_coalesce_regression = _detect_window_group_limit_coalesce_regression(
            stages_raw, row.get('_sql_executions_raw', []), row.get('_executor_summary_raw', {}))

        # Cost-optimized
        max_exec_cost_init, min_exec_cost = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], 0, mem_pct, cpu_pct, idle_pct, spill_gb, mode="cost",
            orig_executors=orig_executors, orig_cores=orig_cores, orig_mem_mb=int(orig_executor_mem_gb * 1024),
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration, stages=stages_raw, is_ec2_source=is_ec2,
        )
        sp_cost, target_mib_cost = auto_tune_custom(shuffle_bytes, max_exec_cost_init)
        max_exec_cost, min_exec_cost = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], sp_cost, mem_pct, cpu_pct, idle_pct, spill_gb, mode="cost",
            orig_executors=orig_executors, orig_cores=orig_cores, orig_mem_mb=int(orig_executor_mem_gb * 1024),
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration, stages=stages_raw, is_ec2_source=is_ec2,
        )
        sp_cost = cap_partitions(sp_cost, max_exec_cost)

        # EMR Serverless: never reduce partitions below 1000 (EMR default + AQE coalesces down)
        # Applies to all paths — target is always Serverless
        sp_cost = max(1000, sp_cost)

        # Bump up worker size if too many executors (shuffle coordination overhead)
        # Always go Small→Medium→Large (never skip Medium)
        _is_rule2_spill = is_ec2 and orig_executors > 150 and sh_ratio < 800 and spill_gb > 5000
        if is_ec2 and max_exec_cost > 70 and worker_type == "Small" and not _is_rule2_spill:
            worker_type = "Medium"
            worker_cfg = {"vcpu": 8, "memory": 54}
            max_exec_cost = max(2, max_exec_cost * 4 // 8)  # preserve total cores
            min_exec_cost = max(1, min(max_exec_cost - 2, max(5, max_exec_cost // 3)))
        if is_ec2 and max_exec_cost > 100 and worker_type == "Medium":
            worker_type = "Large"
            worker_cfg = {"vcpu": 16, "memory": 108}
            max_exec_cost = max(2, max_exec_cost * 8 // 16)  # preserve total cores
            min_exec_cost = max(1, min(max_exec_cost - 2, max(5, max_exec_cost // 3)))

        # Serverless path: bump worker size based on actual shuffle/spill metrics.
        # Serverless event logs have accurate metrics — use them directly.
        if not is_ec2:
            _total_shuffle_gb = s_in_gb + s_out_gb
            _orig_vcpu = worker_cfg["vcpu"]
            if (_total_shuffle_gb > 15000 or spill_gb > 50000) and worker_type != "Large":
                worker_type = "Large"
                worker_cfg = {"vcpu": 16, "memory": 108}
                max_exec_cost = max(2, max_exec_cost * _orig_vcpu // 16)
                min_exec_cost = max(1, min(max_exec_cost - 2, max(5, max_exec_cost // 3)))
            elif (_total_shuffle_gb > 5000 or spill_gb > 10000) and worker_type == "Small":
                worker_type = "Medium"
                worker_cfg = {"vcpu": 8, "memory": 54}
                max_exec_cost = max(2, max_exec_cost * 4 // 8)
                min_exec_cost = max(1, min(max_exec_cost - 2, max(5, max_exec_cost // 3)))

        # Serverless path: right-size partitions and executors from actual metrics
        if not is_ec2 and stages_raw:
            _per_task_mem_gb = worker_cfg["memory"] * 0.6 / worker_cfg["vcpu"]
            # Find peak stage shuffle (max of write and read)
            _peak_shuf_w = max((s.get("shuffle_write_gb", 0) or 0) for s in stages_raw)
            _peak_shuf_r = max((s.get("shuffle_read_gb", 0) or 0) for s in stages_raw)
            _peak_shuf = max(_peak_shuf_w, _peak_shuf_r)

            # Partition sizing: target zero spill (partition fits in per-task memory)
            # Use 70% of per-task memory for build side hash table headroom
            _serverless_spill_override = False
            if spill_gb > _peak_shuf and _peak_shuf > 100:
                _target_partitions = int(math.ceil(_peak_shuf / (_per_task_mem_gb * 0.7)))
                if _target_partitions > sp_cost:
                    sp_cost = _target_partitions
                    _serverless_spill_override = True

            # Executor floor: disk throughput must deliver peak stage IO in 15 min
            _peak_disk_io = _peak_shuf_w + _peak_shuf_r
            _disk_throughput = 0.244  # GB/s per executor
            if _peak_disk_io > 1000:
                _target_stage_sec = 900  # 15 min
                _n_disk = int(math.ceil(_peak_disk_io / (_target_stage_sec * _disk_throughput)))
                if _n_disk > max_exec_cost:
                    max_exec_cost = _n_disk
                    min_exec_cost = max(1, min(max_exec_cost - 2, max(5, max_exec_cost // 3)))

        # Stage-level efficiency: no single stage should take > 60min
        # Uses total_task_time_sec (sum of all task exec times) as total work
        # Apply 0.85x factor for I/O-bound stages (Serverless NVMe is faster than EC2)
        if is_ec2 and stages_raw:
            max_stage = max(stages_raw, key=lambda s: s.get('total_task_time_sec', 0) or 0)
            max_stage_work = max_stage.get('total_task_time_sec', 0) or 0
            # I/O-bound heuristic: stage has shuffle or spill
            stage_has_io = (max_stage.get('shuffle_read_gb', 0) or 0) + (max_stage.get('disk_spill_gb', 0) or 0) > 10
            speedup = 0.85 if stage_has_io else 1.0
            adjusted_work = max_stage_work * speedup
            if adjusted_work > 0:
                target_cores = max_exec_cost * worker_cfg["vcpu"]
                predicted_stage_sec = adjusted_work / target_cores
                if predicted_stage_sec > 1800:  # would take > 30min
                    needed_cores = adjusted_work / 1800
                    needed_exec = int(needed_cores / worker_cfg["vcpu"]) + 1
                    max_exec_cost = needed_exec
                    min_exec_cost = max(1, min(max_exec_cost - 2, max(5, max_exec_cost // 3)))

        # Account for EC2 spill conservatively: Serverless has more memory per core
        # and AQE reduces spill, but not to zero. Use 10% of observed spill as safety floor.
        _eff_disk_spill = disk_spill_gb * 0.1
        _eff_mem_spill = spill_gb * 0.1
        executor_disk_cost = _calculate_executor_disk(s_out_gb, _eff_disk_spill, _eff_mem_spill, max_exec_cost)
        # Performance-optimized
        max_exec_perf_init, min_exec_perf = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], 0, mem_pct, cpu_pct, idle_pct, spill_gb, mode="performance",
            orig_executors=orig_executors, orig_cores=orig_cores, orig_mem_mb=int(orig_executor_mem_gb * 1024),
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration, stages=stages_raw, is_ec2_source=is_ec2,
        )
        sp_perf, target_mib_perf = auto_tune_custom(shuffle_bytes, max_exec_perf_init)
        max_exec_perf, min_exec_perf = _compute_exec_limits(
            i_in_gb, worker_cfg["vcpu"], sp_perf, mem_pct, cpu_pct, idle_pct, spill_gb, mode="performance",
            orig_executors=orig_executors, orig_cores=orig_cores, orig_mem_mb=int(orig_executor_mem_gb * 1024),
            total_task_exec_hours=total_task_exec_hours, duration_hours=duration, stages=stages_raw, is_ec2_source=is_ec2,
        )
        sp_perf = cap_partitions(sp_perf, max_exec_perf)
        # EMR Serverless: never reduce partitions below 1000 (target is always Serverless)
        sp_perf = max(1000, sp_perf)
        # Stage-level efficiency for perf mode: no stage > 30min
        if is_ec2 and stages_raw:
            max_stage_p = max(stages_raw, key=lambda s: s.get('total_task_time_sec', 0) or 0)
            max_work_p = max_stage_p.get('total_task_time_sec', 0) or 0
            stage_io_p = (max_stage_p.get('shuffle_read_gb', 0) or 0) + (max_stage_p.get('disk_spill_gb', 0) or 0) > 10
            adjusted_p = max_work_p * (0.85 if stage_io_p else 1.0)
            if adjusted_p > 0:
                target_cores_p = max_exec_perf * worker_cfg["vcpu"]
                if adjusted_p / target_cores_p > 1800:  # > 30min
                    needed_exec_p = int((adjusted_p / 1800) / worker_cfg["vcpu"]) + 1
                    max_exec_perf = needed_exec_p
                    min_exec_perf = max(1, min(max_exec_perf - 2, max(5, max_exec_perf // 3)))
        executor_disk_perf = _calculate_executor_disk(s_out_gb, _eff_disk_spill, _eff_mem_spill, max_exec_perf)
        
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

        def _driver_disk_sizing(total_tasks):
            """Scale driver disk based on total task count to prevent event log disk pressure."""
            if total_tasks > 500000:
                return "100G"
            elif total_tasks > 100000:
                return "50G"
            else:
                return None

        total_tasks = int(row.get('total_tasks', 0) or 0)

        def _driver_max_result_size(shuffle_gb, partitions, input_gb, driver_mem_gb):
            """Scale driver maxResultSize as 25% of driver memory.
            Only set when data volume suggests default 1G may be insufficient."""
            total_data = shuffle_gb + input_gb
            if total_data > 1000 or partitions > 200:
                return f"{max(2, driver_mem_gb // 4)}g"
            else:
                return None  # default 1g is fine

        def _should_disable_broadcast(stages, spark_config, total_shuffle_write_gb, driver_mem_gb):
            """Detect if broadcast joins should be disabled.
            Only triggers when a stage failed with maxResultSize, shows the
            collect-to-driver pattern, AND projected total exceeds 50% of driver memory.
            """
            import re
            if not stages:
                return False
            abjt = spark_config.get('spark.sql.autoBroadcastJoinThreshold', '')
            if str(abjt) == '-1':
                return False
            for st in stages:
                reason = st.get('failure_reason', '') or ''
                if ('maxResultSize' in reason
                    and st.get('shuffle_read_gb', 0) > 1.0
                    and st.get('shuffle_write_gb', 0) == 0
                    and st.get('input_gb', 0) == 0):
                    match = re.search(r"(\d+) tasks \(([\d.]+) (GiB|MiB)", reason)
                    if match:
                        tasks_at_failure = int(match.group(1))
                        size_gb = float(match.group(2))
                        if match.group(3) == 'MiB':
                            size_gb /= 1024
                        total_tasks = st.get('num_tasks', tasks_at_failure)
                        projected_gb = (size_gb / max(tasks_at_failure, 1)) * total_tasks
                        if projected_gb > driver_mem_gb * 0.5:
                            return True
                    else:
                        return True
            return False

        def build_spark_cfg(max_exec, min_exec, sp, executor_disk, vcpu_override=None, mem_override=None):
            d_cores, d_mem = _driver_sizing(sp, max_exec, s_in_gb + s_out_gb)
            driver_disk = _driver_disk_sizing(total_tasks)
            vcpu = vcpu_override or worker_cfg["vcpu"]
            mem = mem_override or worker_cfg["memory"]
            _adv_raw = _spark_config_raw.get('spark.sql.adaptive.advisoryPartitionSizeInBytes')
            _adv_bytes_for_mpb = _parse_size_to_bytes(_adv_raw)
            cfg = {
                "spark.driver.cores": str(d_cores),
                "spark.driver.memory": f"{d_mem}G",
                "spark.executor.cores": str(vcpu),
                "spark.executor.memory": f"{mem}g",
                "spark.dynamicAllocation.enabled": "true",
                "spark.sql.adaptive.enabled": "true",
                "spark.sql.adaptive.coalescePartitions.parallelismFirst": "false",
                "spark.sql.files.maxPartitionBytes": _max_partition_bytes(i_in_gb, _adv_bytes_for_mpb),
                **({"spark.emr-serverless.executor.disk": executor_disk,
                "spark.emr-serverless.executor.disk.type": "shuffle_optimized"} if executor_disk else {}),
                "spark.sql.shuffle.partitions": str(sp),
                "spark.dynamicAllocation.maxExecutors": str(max_exec),
                "spark.dynamicAllocation.minExecutors": str(min_exec),
                "spark.dynamicAllocation.initialExecutors": str(min_exec),
            }
            if driver_disk:
                cfg["spark.emr-serverless.driver.disk"] = driver_disk
            max_result = _driver_max_result_size(s_in_gb + s_out_gb, sp, i_in_gb, d_mem)
            if max_result:
                cfg["spark.driver.maxResultSize"] = max_result
            # Detect broadcast-induced maxResultSize failures:
            # A stage that reads shuffle, writes nothing (collect to driver),
            # Broadcast: only override when explicitly needed
            # Setting broadcastJoinThreshold explicitly causes AQE re-optimization overhead
            # at every stage boundary (confirmed: 6x regression on TPC-DS q92 with 30m vs not set)
            if _should_disable_broadcast(stages_raw, _spark_config_raw, s_out_gb, d_mem):
                cfg["spark.sql.autoBroadcastJoinThreshold"] = "-1"
            # else: don't set — let EMR/AQE handle dynamically
            # Advisory partition size: compute safe value based on per-task memory
            adv = _spark_config_raw.get('spark.sql.adaptive.advisoryPartitionSizeInBytes')
            per_task_mem_gb = worker_cfg["memory"] * 0.6 / worker_cfg["vcpu"]
            # Safe advisory = per_task_mem * 0.5 (accounts for hash join expansion ~2-3x)
            safe_advisory_bytes = int(per_task_mem_gb * 0.5 * 1024 * 1024 * 1024)
            
            # Determine if spill is significant relative to shuffle volume
            shuffle_total_gb = max(s_in_gb + s_out_gb, 1)
            spill_ratio = spill_gb / shuffle_total_gb
            
            if spill_ratio > 0.05:  # spill > 5% of shuffle = advisory likely too large
                # Do NOT set advisory (let AQE use default 64MB, coalesces conservatively)
                pass
            elif adv:
                adv_bytes = _parse_size_to_bytes(adv)
                if adv_bytes <= safe_advisory_bytes:
                    cfg['spark.sql.adaptive.advisoryPartitionSizeInBytes'] = str(adv)
                # else: don't set (too large, would cause spill)
            
            # Partitions: if spill is significant relative to shuffle, compute from zero-spill formula
            if spill_ratio > 0.05:
                # Zero-spill target: peak_shuffle / (per_task_mem * 0.7)
                peak_shuffle_gb = max(s_in_gb, s_out_gb)
                zero_spill_parts = int(peak_shuffle_gb / (per_task_mem_gb * 0.7))
                zero_spill_parts = max(1000, zero_spill_parts)
                sp = zero_spill_parts
                cfg['spark.sql.shuffle.partitions'] = str(sp)
                # MaxExecutors: size for 2-3 waves (avoid 4-6 waves of waiting)
                target_waves = 4
                needed_exec = int(sp / (worker_cfg["vcpu"] * target_waves))
                needed_exec = max(needed_exec, max_exec)  # don't reduce below existing
                if needed_exec > max_exec:
                    cfg['spark.dynamicAllocation.maxExecutors'] = str(needed_exec)
                    cfg['spark.dynamicAllocation.minExecutors'] = str(max(3, needed_exec // 3))
                    cfg['spark.dynamicAllocation.initialExecutors'] = str(max(3, needed_exec // 3))
            elif s_out_gb > 10 and is_ec2:
                # Compute advisory: target 6 waves of tasks per core for good parallelism
                target_tasks = max_exec * worker_cfg["vcpu"] * 6
                advisory_bytes = int(s_out_gb * 1024 * 1024 * 1024 / max(target_tasks, 1))
                advisory_bytes = max(128 * 1024 * 1024, min(1024 * 1024 * 1024, advisory_bytes))  # 128MB-1GB
                cfg['spark.sql.adaptive.advisoryPartitionSizeInBytes'] = str(advisory_bytes)
            # Preserve AQE coalescing settings from source when set
            # Stability-first: don't override AQE skew/rebalance settings (let EMR defaults handle)
            # Only set parallelismFirst=false (proven safe, -24% runtime improvement)
            cfg['spark.sql.adaptive.coalescePartitions.parallelismFirst'] = 'false'
            cfg.update(_get_timeout_configs(i_in_gb, duration))
            cfg.update(_get_s3_retry_configs(i_in_gb, i_out_gb))
            cfg.update(_get_iceberg_configs())
            if sh_ratio > 30:
                cfg.update({"spark.shuffle.compress": "true", "spark.shuffle.spill.compress": "true"})

            # Serverless: recommend zstd when disk-bound with CPU headroom
            # zstd gives 50-100% better compression than lz4, reducing disk IO significantly
            if not is_ec2 and shuffle_fetch_wait_pct > 20 and cpu_pct < 50:
                cfg["spark.io.compression.codec"] = "zstd"
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
        
        # Inject WindowGroupLimit recommendation if detected
        if window_skew_findings or window_coalesce_regression:
            excluded_rule = 'org.apache.spark.sql.catalyst.optimizer.InferWindowGroupLimit'
            wgl_type = 'window_group_limit_skew' if window_skew_findings else 'window_group_limit_coalesce_regression'
            wgl_msg = (f'Stage(s) with extreme skew from InferWindowGroupLimit optimizer rule. '
                       f'{len(window_skew_findings)} stage(s) affected, '
                       f'worst: stage {window_skew_findings[0]["stage_id"]} '
                       f'({window_skew_findings[0]["pct_of_job"]}% of job time, '
                       f'{window_skew_findings[0]["skew_ratio"]}x skew ratio).') if window_skew_findings else (
                       'InferWindowGroupLimit causing AQE coalescing imbalance with window functions. '
                       'Small coalesced stages near failed shuffle stages indicate uneven partition distributions.')
            cost_rec['bottleneck_warnings'] = [{
                'type': wgl_type,
                'severity': 'HIGH',
                'message': wgl_msg,
                'recommendation': f'spark.sql.optimizer.excludedRules={excluded_rule}',
                'affected_stages': window_skew_findings if window_skew_findings else [],
            }]
            perf_rec['bottleneck_warnings'] = cost_rec['bottleneck_warnings']
            # Add to spark_configs
            existing = cost_rec['spark_configs'].get('spark.sql.optimizer.excludedRules', '')
            rules = f'{existing},{excluded_rule}' if existing else excluded_rule
            cost_rec['spark_configs']['spark.sql.optimizer.excludedRules'] = rules
            perf_rec['spark_configs']['spark.sql.optimizer.excludedRules'] = rules

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
        if shuffle_fetch_wait_pct > 50 and spill_gb < 20000:
            # IOPS-based: 5 MB/s effective per disk for shuffle random IO
            # Target: shuffle IO completes within 30% of job duration
            # Skip when spill > 20TB — high spill + fetch wait indicates capacity/OOM issue,
            # not disk throughput. Downsizing would make OOM worse.
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
            saved_warnings = cost_recs[-1].get('bottleneck_warnings')
            cost_recs[-1] = dict(io_rec)
            cost_recs[-1]["optimization_mode"] = "cost"
            if saved_warnings:
                cost_recs[-1]['bottleneck_warnings'] = saved_warnings
                excluded = saved_warnings[0].get('recommendation', '')
                if excluded:
                    cost_recs[-1]['spark_configs'][excluded.split('=')[0]] = excluded.split('=', 1)[1]
            # Perf mode: keep large workers if they already have enough disks.
            # Otherwise, find the smallest worker type that meets the disk target
            # while preserving total core count — smaller workers are cheaper.
            if max_exec_perf < io_max:
                perf_orig_cores = max_exec_perf * worker_cfg["vcpu"]
                best = None
                for try_type in ["Small", "Medium", "Large"]:
                    tr = WORKER_RANGES_IO[try_type]
                    need_exec = io_max  # must match cost disk count
                    total_cores = need_exec * tr["vcpu"]
                    if total_cores >= perf_orig_cores:
                        per_task_mem = worker_cfg["memory"] / worker_cfg["vcpu"]
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
                    perf_recs[-1]["worker"]["total_vcpu_capacity"] = max_exec_perf * worker_cfg["vcpu"]
                    perf_recs[-1]["worker"]["total_memory_capacity"] = max_exec_perf * worker_cfg["memory"]
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


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Dual-mode EMR Serverless recommender (S3 or Local)")
    parser.add_argument("--input-path", required=True, help="S3 path (s3://bucket/prefix) or local path")
    parser.add_argument("--region", default="us-east-1", help="AWS region (for S3 only)")
    parser.add_argument("--limit", type=int, default=100, help="Max applications")
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
