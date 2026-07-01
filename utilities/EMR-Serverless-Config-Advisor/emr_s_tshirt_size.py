#!/usr/bin/env python3
"""
EMR Serverless T-Shirt Size Recommender

Structure:
  Size (T-shirt):    XS | S | M | L | XL
  Sub-category:      General | Optimized | IO-Optimized
  Special:           Iceberg Maintenance

- General:      Balanced defaults, 1-wave partitions, 200G disk
- Optimized:    Heavy workloads (joins, shuffle, aggregation), 2-wave partitions, 1000G disk
- IO-Optimized: Optimized + one tier smaller worker × 2 executors (max disk parallelism)

Usage:
  python3 emr_s_tshirt_size.py --size M
  python3 emr_s_tshirt_size.py --size L --sub-category Optimized
  python3 emr_s_tshirt_size.py --size M --format spark-submit
"""
import math
from dataclasses import dataclass, field
from typing import Dict, Optional

# WGL exclusion — defensive; prevents infinite loop on EMR 7.x window functions
WGL_RULE = "org.apache.spark.sql.catalyst.optimizer.InferWindowGroupLimit"

# ─── T-Shirt Sizes ───────────────────────────────────────────────────────────
SIZES = ["XS", "S", "M", "L", "XL"]
SUB_CATEGORIES = ["General", "Optimized", "IO-Optimized", "Iceberg-Maintenance"]

# Generous defaults when user provides no sizing input.
# Dynamic allocation scales down unused — no cost penalty for over-setting.
DEFAULT_MAX_EXECUTORS = {"XS": 3, "S": 50, "M": 100, "L": 200, "XL": 500}


@dataclass
class WorkloadIntent:
    input_size_gb: float = 100.0
    workload_type: str = "etl"
    num_joins: int = 5
    largest_table_gb: float = 50.0
    is_compaction: bool = False
    target_duration_minutes: Optional[int] = None
    shuffle_write_gb: Optional[float] = None
    shuffle_ratio_pct: Optional[float] = None
    shj_count: Optional[int] = None
    fan_out_factor: Optional[float] = None
    num_files: Optional[int] = None
    task_hours: Optional[float] = None
    actual_duration_hours: Optional[float] = None


@dataclass
class BucketResult:
    size: str
    sub_bucket: str
    worker_type: str
    configs: Dict[str, str] = field(default_factory=dict)
    rationale: str = ""

    @property
    def label(self):
        return f"{self.size}/{self.sub_bucket}"


# ─── Selection Logic ──────────────────────────────────────────────────────────

def select_bucket(intent: WorkloadIntent) -> BucketResult:
    """Select size + sub-category from workload intent."""
    if intent.workload_type == "iceberg_maintenance" or intent.is_compaction:
        return _iceberg_maintenance(intent)

    size = _classify_size(intent)
    if size == "XS":
        return _xs(intent)

    sub = _classify_sub_category(intent)
    builders = {
        "General": _general,
        "Optimized": _optimized,
        "IO-Optimized": _io_optimized,
    }
    return builders[sub](size, intent)


def _classify_size(intent: WorkloadIntent) -> str:
    gb = intent.input_size_gb
    # Critique #2: explode jobs have tiny input but massive shuffle — size by shuffle
    if intent.shuffle_write_gb and intent.shuffle_write_gb > gb * 10:
        gb = intent.shuffle_write_gb / 4  # treat as if input is 25% of shuffle volume
    if gb <= 5 and (intent.target_duration_minutes is None or intent.target_duration_minutes <= 5):
        if intent.workload_type in ("micro", "etl", "compaction", "iceberg_maintenance"):
            return "XS"
        return "S"
    elif gb <= 100:
        return "S"
    elif gb <= 1000:
        return "M"
    elif gb <= 5000:
        return "L"
    return "XL"


def _classify_sub_category(intent: WorkloadIntent) -> str:
    """Determine sub-category from workload signals."""
    # IO-Optimized: tiny input with massive fan-out
    if intent.input_size_gb < 10 and (
        (intent.fan_out_factor and intent.fan_out_factor > 100) or
        (intent.shuffle_ratio_pct and intent.shuffle_ratio_pct > 10000)
    ):
        return "IO-Optimized"

    # Optimized: heavy shuffle, many joins, or large aggregation
    if intent.shuffle_write_gb and intent.shuffle_write_gb > 1000:
        return "Optimized"
    if intent.shuffle_ratio_pct and intent.shuffle_ratio_pct >= 30:
        return "Optimized"
    if intent.num_joins >= 20 or (intent.shj_count and intent.shj_count >= 40):
        return "Optimized"
    if intent.workload_type in ("aggregation", "join_heavy") and intent.input_size_gb > 500:
        return "Optimized"

    return "General"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _pick_worker(max_exec: int) -> dict:
    """Bump worker size to reduce N² shuffle coordination overhead."""
    if max_exec > 200:
        return {"cores": 16, "mem": "108G", "label": "large"}
    if max_exec > 70:
        return {"cores": 8, "mem": "54G", "label": "medium"}
    return {"cores": 4, "mem": "27G", "label": "small"}


def _resolve_max_executors(size: str, intent: WorkloadIntent, cores: int) -> int:
    """3 modes for maxExecutors:
    Mode 3 (best): Event log → task_hours / (target × packing × cores)
    Mode 2 (good): Proxy → input_gb / (throughput × target × packing)
    Mode 1 (safe): No input → generous default per size

    Three constraints (Mode 2/3):
      - compute_floor: enough cores to finish work in target time
      - serving_floor: enough hosts to serve shuffle at 0.04 GB/s/host (network)
      - disk_floor: enough executors to write/read shuffle at 0.244 GB/s/executor (disk)
    """
    PACKING = 0.70
    NETWORK_SERVING_GBPS = 0.04   # per host — above this → fetch timeouts
    DISK_THROUGHPUT_GBPS = 0.244  # per executor on shuffle_optimized NVMe

    # Mode 3: Event log
    if intent.task_hours is not None and intent.task_hours > 0:
        target_h = (intent.target_duration_minutes / 60.0) if intent.target_duration_minutes else 1.0
        target_sec = target_h * 3600
        n = math.ceil(intent.task_hours / (target_h * PACKING * cores))
        if intent.shuffle_write_gb and intent.shuffle_write_gb > 1000:
            serving = math.ceil(intent.shuffle_write_gb / (NETWORK_SERVING_GBPS * target_sec))
            disk = math.ceil(intent.shuffle_write_gb / (DISK_THROUGHPUT_GBPS * target_sec))
            n = max(n, serving, disk)
        return max(10, n)

    # Mode 2: Proxy
    if intent.target_duration_minutes is not None:
        target_sec = intent.target_duration_minutes * 60
        throughput = 0.5 * cores
        work_min = intent.input_size_gb / max(0.1, throughput)
        n = math.ceil(work_min / (intent.target_duration_minutes * 0.50))
        if intent.shuffle_write_gb and intent.shuffle_write_gb > 1000:
            serving = math.ceil(intent.shuffle_write_gb / (NETWORK_SERVING_GBPS * target_sec))
            disk = math.ceil(intent.shuffle_write_gb / (DISK_THROUGHPUT_GBPS * target_sec))
            n = max(n, serving, disk)
        return max(10, n)

    # Mode 1: Generous default
    return DEFAULT_MAX_EXECUTORS[size]


def _shuffle_partitions(n: int, cores: int, waves: int = 1,
                        shuffle_write_gb: float = 0) -> int:
    """Compute shuffle partitions balancing parallelism vs IOPS.

    Key insight: raw shuffle wire bytes overstate in-memory partition size by ~5×
    (shuffle records are typically 500-1000 bytes vs 5-15 KB decompressed rows).
    Targeting ~5 GB of raw shuffle per partition keeps in-memory footprint ~1 GB,
    while minimizing shuffle block count (fewer IOPS operations).

    Examples:
      65 TB shuffle → 13,000 partitions (5 GB raw / ~1 GB in-memory each)
      4 TB shuffle  → 4,000 partitions (parallelism floor wins)
      50 GB shuffle → 480 partitions (parallelism floor wins)

    Floor: waves × n × cores (ensures enough tasks for full parallelism).
    """
    parallelism = waves * n * cores

    if shuffle_write_gb > 0:
        # Target ~5 GB raw shuffle per partition ≈ 1 GB in-memory
        # (shuffle wire bytes include serialization overhead, hash headers,
        #  and are typically 3-5× the deserialized in-memory size)
        by_size = max(200, int(math.ceil(shuffle_write_gb / 5.0)))
        # Use the larger of parallelism floor and size-based
        # Minimum 1000 — EMR Serverless default + AQE coalesces down from here
        return max(1000, max(parallelism, by_size))
    else:
        # Fallback: parallelism-based, capped at 10K
        return max(1000, min(parallelism, 10000))


def _max_partition_bytes(input_gb: float) -> str:
    if input_gb < 10: return "64m"
    if input_gb < 500: return "128m"
    if input_gb < 3000: return "256m"
    return "512m"


def _executor_disk(shuffle_gb: float, max_exec: int) -> str:
    """Right-size disk: shuffle per executor × 3 safety margin (accounts for
    multi-stage accumulation, spill, and skew). Min 200G, max 2000G.
    Minimum 500G when shuffle is significant (>100GB)."""
    if shuffle_gb <= 0 or max_exec <= 0:
        return "200G"
    per_exec = shuffle_gb / max_exec * 3
    min_disk = 500 if shuffle_gb > 100 else 200
    disk = max(min_disk, min(2000, int(math.ceil(per_exec / 20) * 20)))
    return f"{disk}G"


def _driver_sizing(worker_cores: int) -> tuple:
    """Driver matches worker tier, capped at 8c/54G (driver doesn't run tasks)."""
    if worker_cores >= 8:
        return "8", "54G"
    elif worker_cores >= 4:
        return "4", "27G"
    return "1", "2G"


def _s3_retry_configs(input_gb: float) -> Dict[str, str]:
    """S3 retry configs for large jobs that hit S3 hard."""
    if input_gb >= 1000:
        return {"spark.hadoop.fs.s3a.retry.limit": "15", "spark.hadoop.fs.s3a.attempts.maximum": "15"}
    elif input_gb >= 100:
        return {"spark.hadoop.fs.s3a.retry.limit": "10", "spark.hadoop.fs.s3a.attempts.maximum": "10"}
    return {}


def _base_configs() -> Dict[str, str]:
    return {
        "spark.dynamicAllocation.enabled": "true",
        "spark.sql.optimizer.excludedRules": WGL_RULE,
        "spark.sql.adaptive.coalescePartitions.parallelismFirst": "false",
    }


# ─── XS ──────────────────────────────────────────────────────────────────────

def _xs(intent: WorkloadIntent) -> BucketResult:
    configs = {
        **_base_configs(),
        "spark.executor.cores": "1",
        "spark.executor.memory": "2G",
        "spark.driver.cores": "1",
        "spark.driver.memory": "2G",
        "spark.dynamicAllocation.maxExecutors": "3",
        "spark.dynamicAllocation.minExecutors": "1",
        "spark.dynamicAllocation.initialExecutors": "1",
        "spark.sql.shuffle.partitions": "20",
        "spark.sql.files.maxPartitionBytes": "32m",
    }
    return BucketResult("XS", "General", "micro", configs,
        rationale="Micro job (describe, count, SCD2, catalog ops) — minimal resources")


# ─── Iceberg Maintenance ──────────────────────────────────────────────────────

def _iceberg_maintenance(intent: WorkloadIntent) -> BucketResult:
    num_files = intent.num_files or max(20, int(intent.input_size_gb * 1024 / 100))
    max_exec = min(100, max(5, math.ceil(num_files / 20)))
    configs = {
        **_base_configs(),
        "spark.executor.cores": "4",
        "spark.executor.memory": "14G",
        "spark.driver.cores": "4",
        "spark.driver.memory": "14G",
        "spark.dynamicAllocation.maxExecutors": str(max_exec),
        "spark.sql.shuffle.partitions": "1000",
        "spark.sql.files.maxPartitionBytes": "512m",
        "spark.emr-serverless.executor.disk": "200G",
        "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
    }
    return BucketResult("M", "Iceberg-Maintenance", "small", configs,
        rationale=f"Iceberg maintenance — scale by file count ({num_files} files)")


# ─── General ─────────────────────────────────────────────────────────────────

def _general(size: str, intent: WorkloadIntent) -> BucketResult:
    n = _resolve_max_executors(size, intent, 4)
    w = _pick_worker(n)
    if w["cores"] > 4:
        n = math.ceil(n * 4 / w["cores"])
    parts = _shuffle_partitions(n, w["cores"], waves=1,
                               shuffle_write_gb=intent.shuffle_write_gb or 0)
    drv_cores, drv_mem = _driver_sizing(w["cores"])
    configs = {
        **_base_configs(),
        "spark.executor.cores": str(w["cores"]),
        "spark.executor.memory": w["mem"],
        "spark.driver.cores": drv_cores,
        "spark.driver.memory": drv_mem,
        "spark.dynamicAllocation.maxExecutors": str(n),
        "spark.sql.shuffle.partitions": str(parts),
        "spark.sql.files.maxPartitionBytes": _max_partition_bytes(intent.input_size_gb),
        "spark.emr-serverless.executor.disk": "200G",
        "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
        "spark.network.timeout": "1200s",
        **_s3_retry_configs(intent.input_size_gb),
    }
    return BucketResult(size, "General", w["label"], configs,
        rationale="Balanced defaults — suitable for most workloads")


# ─── Optimized ────────────────────────────────────────────────────────────────

def _optimized(size: str, intent: WorkloadIntent) -> BucketResult:
    n = _resolve_max_executors(size, intent, 4)
    w = _pick_worker(n)
    if w["cores"] > 4:
        n = math.ceil(n * 4 / w["cores"])
    # Disk capacity floor: shuffle per executor must fit in 70% of disk
    shuffle_gb = intent.shuffle_write_gb or 0
    disk = _executor_disk(shuffle_gb, n)
    disk_val = int(disk.replace("G", ""))
    if shuffle_gb > 0:
        capacity_floor = math.ceil(shuffle_gb / (disk_val * 0.7))
        n = max(n, capacity_floor)
        disk = _executor_disk(shuffle_gb, n)  # recalc after floor bump
    parts = _shuffle_partitions(n, w["cores"], waves=2, shuffle_write_gb=shuffle_gb)
    drv_cores, drv_mem = _driver_sizing(w["cores"])
    configs = {
        **_base_configs(),
        "spark.executor.cores": str(w["cores"]),
        "spark.executor.memory": w["mem"],
        "spark.driver.cores": drv_cores,
        "spark.driver.memory": drv_mem,
        "spark.dynamicAllocation.maxExecutors": str(n),
        "spark.sql.shuffle.partitions": str(parts),
        "spark.sql.files.maxPartitionBytes": _max_partition_bytes(intent.input_size_gb),
        "spark.emr-serverless.executor.disk": disk,
        "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
        "spark.network.timeout": "1200s",
        **_s3_retry_configs(intent.input_size_gb),
    }
    return BucketResult(size, "Optimized", w["label"], configs,
        rationale="Heavy workload — more partitions and disk for shuffle/joins/aggregation")


# ─── IO-Optimized ─────────────────────────────────────────────────────────────

def _io_optimized(size: str, intent: WorkloadIntent) -> BucketResult:
    # Start from Optimized config, then downsize worker and double executors
    n = _resolve_max_executors(size, intent, 4)
    w = _pick_worker(n)
    if w["cores"] > 4:
        n = math.ceil(n * 4 / w["cores"])
    # Downsize one tier, double executors (more hosts = more disk throughput)
    if w["cores"] == 16:
        cores, mem, label = 8, "54G", "medium"
        n = n * 2
    elif w["cores"] == 8:
        cores, mem, label = 4, "27G", "small"
        n = n * 2
    else:
        cores, mem, label = 4, "27G", "small"
        n = n * 2
    # Disk capacity floor
    shuffle_gb = intent.shuffle_write_gb or 0
    disk = _executor_disk(shuffle_gb, n)
    disk_val = int(disk.replace("G", ""))
    if shuffle_gb > 0:
        capacity_floor = math.ceil(shuffle_gb / (disk_val * 0.7))
        n = max(n, capacity_floor)
        disk = _executor_disk(shuffle_gb, n)
    parts = _shuffle_partitions(n, cores, waves=2, shuffle_write_gb=shuffle_gb)
    # IO-Opt maxPartitionBytes: scale by size (not input_gb which is tiny for fan-out)
    io_mpb = {"S": "128m", "M": "128m", "L": "128m", "XL": "256m"}.get(size, "128m")
    drv_cores, drv_mem = _driver_sizing(cores)
    configs = {
        **_base_configs(),
        "spark.executor.cores": str(cores),
        "spark.executor.memory": mem,
        "spark.driver.cores": drv_cores,
        "spark.driver.memory": drv_mem,
        "spark.dynamicAllocation.maxExecutors": str(n),
        "spark.sql.shuffle.partitions": str(parts),
        "spark.sql.files.maxPartitionBytes": io_mpb,
        "spark.emr-serverless.executor.disk": disk,
        "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
        "spark.network.timeout": "1200s",
        **_s3_retry_configs(intent.input_size_gb),
    }
    return BucketResult(size, "IO-Optimized", label, configs,
        rationale="Massive fan-out — smaller workers × more hosts for disk parallelism")


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser(description="EMR Serverless T-Shirt Size Recommender")
    p.add_argument("--size", choices=SIZES, help="T-shirt size (XS/S/M/L/XL)")
    p.add_argument("--sub-category", default="General", choices=SUB_CATEGORIES)
    p.add_argument("--input-size-gb", type=float, help="Input data size in GB")
    p.add_argument("--shuffle-write-gb", type=float, help="Estimated shuffle volume in GB (for heavy joins/aggregations)")
    p.add_argument("--fan-out-factor", type=float, help="Estimated output amplification (for EXPLODE/CROSS JOIN, e.g. 500)")
    p.add_argument("--target-duration-minutes", type=int, help="Target job runtime in minutes (e.g. current EC2 runtime)")
    p.add_argument("--num-files", type=int, help="Number of files (for Iceberg-Maintenance)")
    p.add_argument("--format", choices=["json", "spark-submit", "table"], default="table")
    args = p.parse_args()

    if not args.size:
        if args.sub_category == "Iceberg-Maintenance" and args.num_files:
            if args.num_files <= 500: args.size = "S"
            elif args.num_files <= 5000: args.size = "M"
            elif args.num_files <= 20000: args.size = "L"
            else: args.size = "XL"
        else:
            p.error("--size is required (unless using Iceberg-Maintenance with --num-files)")

    # Build intent
    input_gb = args.input_size_gb or {"XS": 1, "S": 50, "M": 500, "L": 2500, "XL": 10000}[args.size]
    intent = WorkloadIntent(
        input_size_gb=input_gb,
        is_compaction=(args.sub_category == "Iceberg-Maintenance"),
        num_files=args.num_files,
        shuffle_write_gb=args.shuffle_write_gb,
        fan_out_factor=args.fan_out_factor,
        target_duration_minutes=args.target_duration_minutes,
    )

    # Route
    if args.sub_category == "Iceberg-Maintenance":
        result = _iceberg_maintenance(intent)
    elif args.size == "XS":
        result = _xs(intent)
    else:
        # Re-classify size when shuffle signals indicate larger workload
        effective_size = _classify_size(intent)
        if SIZES.index(effective_size) > SIZES.index(args.size):
            args.size = effective_size
        # Auto-select sub-category when shuffle/fan-out signals are provided
        sub = args.sub_category
        if sub == "General" and (args.shuffle_write_gb or args.fan_out_factor):
            sub = _classify_sub_category(intent)
        builders = {"General": _general, "Optimized": _optimized, "IO-Optimized": _io_optimized}
        result = builders[sub](args.size, intent)

    if args.format == "json":
        print(json.dumps({"size": result.size, "sub_category": result.sub_bucket,
                          "spark_configs": result.configs,
                          "spark_submit_params": " ".join(f"--conf {k}={v}" for k, v in result.configs.items())}, indent=2))
    elif args.format == "spark-submit":
        print(" ".join(f"--conf {k}={v}" for k, v in result.configs.items()))
    else:
        print(f"\n  Bucket: {result.label}\n")
        for k, v in result.configs.items():
            print(f"  {k:<55} = {v}")
        print()
