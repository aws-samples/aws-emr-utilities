#!/usr/bin/env python3
"""
EMR Serverless Sizing Buckets v2

Structure:
  Size (T-shirt):    XS | S | M | L | XL
  Sub-bucket:        General | Memory-Optimized | Shuffle-Optimized | IO-Optimized | Compute-Optimized
  Special category:  Iceberg Maintenance (compaction, expire snapshots, rewrite manifests)

Default is always General within each size.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, Optional

# WGL exclusion — defensive; prevents infinite loop on EMR 7.x window functions
WGL_RULE = "org.apache.spark.sql.catalyst.optimizer.InferWindowGroupLimit"

# ─── T-Shirt Sizes ───────────────────────────────────────────────────────────
SIZES = {
    "XS": {"input_range": (0, 5),       "desc": "Micro jobs (<5GB, <5min)"},
    "S":  {"input_range": (5, 100),      "desc": "Small jobs (5-100GB)"},
    "M":  {"input_range": (100, 1000),   "desc": "Medium jobs (100GB-1TB)"},
    "L":  {"input_range": (1000, 5000),  "desc": "Large jobs (1-5TB)"},
    "XL": {"input_range": (5000, 99999), "desc": "Extra-large jobs (>5TB)"},
}

SUB_BUCKETS = ["General", "Memory-Optimized", "Shuffle-Optimized", "IO-Optimized", "Compute-Optimized"]


@dataclass
class WorkloadIntent:
    input_size_gb: float = 100.0
    workload_type: str = "etl"  # etl | aggregation | join_heavy | compaction | micro | iceberg_maintenance
    num_joins: int = 5
    largest_table_gb: float = 50.0
    is_compaction: bool = False
    target_duration_minutes: Optional[int] = None  # None = use generous default; set = compute precise
    shuffle_write_gb: Optional[float] = None
    shuffle_ratio_pct: Optional[float] = None
    shj_count: Optional[int] = None
    fan_out_factor: Optional[float] = None
    num_files: Optional[int] = None  # for iceberg maintenance
    # Event-log fine-tuning (when available — overrides all heuristics)
    task_hours: Optional[float] = None  # total executor run time in hours
    actual_duration_hours: Optional[float] = None  # actual wall-clock from event log


@dataclass
class BucketResult:
    size: str          # XS, S, M, L, XL
    sub_bucket: str    # General, Memory-Optimized, etc.
    worker_type: str   # small, medium
    configs: Dict[str, str] = field(default_factory=dict)
    rationale: str = ""

    @property
    def label(self):
        return f"{self.size}/{self.sub_bucket}"


# ─── Selection Logic ──────────────────────────────────────────────────────────

def select_bucket(intent: WorkloadIntent) -> BucketResult:
    """Select size + sub-bucket from workload intent."""

    # Special: Iceberg Maintenance
    if intent.workload_type == "iceberg_maintenance" or intent.is_compaction:
        return _iceberg_maintenance(intent)

    # Determine T-shirt size
    size = _classify_size(intent)

    # XS is always General (no sub-bucket differentiation needed)
    if size == "XS":
        return _xs(intent)

    # Determine sub-bucket
    sub = _classify_sub_bucket(intent)

    # Dispatch
    builders = {
        "General": _general,
        "Memory-Optimized": _memory_optimized,
        "Shuffle-Optimized": _shuffle_optimized,
        "IO-Optimized": _io_optimized,
        "Compute-Optimized": _compute_optimized,
    }
    return builders[sub](size, intent)


def _classify_size(intent: WorkloadIntent) -> str:
    gb = intent.input_size_gb
    if gb <= 5 and (intent.target_duration_minutes is None or intent.target_duration_minutes <= 5):
        # Only XS if it's truly a micro job — aggregation/join with tiny input is NOT micro
        if intent.workload_type in ("micro", "etl", "compaction", "iceberg_maintenance"):
            return "XS"
        return "S"  # fan-out / aggregation on small data → treat as S minimum
    elif gb <= 100:
        return "S"
    elif gb <= 1000:
        return "M"
    elif gb <= 5000:
        return "L"
    return "XL"


def _classify_sub_bucket(intent: WorkloadIntent) -> str:
    """Determine optimization axis from workload signals."""
    shj = intent.shj_count or 0

    # Memory-Optimized: many joins or wide schema
    if shj >= 40 or intent.num_joins >= 20:
        return "Memory-Optimized"

    # IO-Optimized: tiny input with massive fan-out
    if intent.input_size_gb < 10 and (
        (intent.fan_out_factor and intent.fan_out_factor > 100) or
        (intent.shuffle_ratio_pct and intent.shuffle_ratio_pct > 10000)
    ):
        return "IO-Optimized"

    # Shuffle-Optimized: heavy shuffle
    if intent.shuffle_write_gb and intent.shuffle_write_gb > 1000:
        return "Shuffle-Optimized"
    if intent.shuffle_ratio_pct and intent.shuffle_ratio_pct >= 30:
        return "Shuffle-Optimized"
    if intent.workload_type == "aggregation" and intent.input_size_gb > 500:
        return "Shuffle-Optimized"
    if intent.workload_type == "join_heavy" and intent.input_size_gb > 500:
        return "Shuffle-Optimized"

    # Compute-Optimized: pure ETL, low shuffle
    if intent.workload_type == "etl":
        return "Compute-Optimized"

    return "General"


# ─── Helpers ──────────────────────────────────────────────────────────────────

# Generous defaults when user provides NO sizing input (safe, won't under-provision)
# Dynamic allocation scales down unused — no cost penalty for over-setting.
DEFAULT_MAX_EXECUTORS = {
    "XS": 3,
    "S":  50,
    "M":  100,
    "L":  200,
    "XL": 500,
}


def _pick_worker(max_exec: int) -> dict:
    if max_exec > 70:
        return {"cores": 8, "mem": "54G", "label": "medium"}
    return {"cores": 4, "mem": "27G", "label": "small"}


def _est_executors(input_gb: float, cores: int, target_min: int, multiplier: float = 1.0) -> int:
    """Compute maxExecutors from proxy: input_size + target_duration."""
    throughput = 0.5 * cores  # conservative: 0.5 GB/min/core
    work_min = input_gb / max(0.1, throughput)
    n = math.ceil(work_min / (target_min * 0.50))  # 50% packing efficiency
    return max(10, int(n * multiplier))


def _resolve_max_executors(size: str, intent: WorkloadIntent, cores: int, multiplier: float = 1.0) -> int:
    """3 modes for maxExecutors:
    
    Mode 3 (best): Event log → task_hours / (target × packing × cores)
    Mode 2 (good):  Proxy questions → input_gb / (throughput × target × packing)
    Mode 1 (safe):  No input → generous default per size
    """
    PACKING = 0.70  # same as job-level recommender

    # Mode 3: Event log available — use actual task-hours (same formula as job-level)
    if intent.task_hours is not None and intent.task_hours > 0:
        if intent.target_duration_minutes:
            target_h = intent.target_duration_minutes / 60.0
        elif intent.actual_duration_hours:
            target_h = intent.actual_duration_hours  # match original duration
        else:
            target_h = 1.0
        n = math.ceil(intent.task_hours / (target_h * PACKING * cores))
        # Also check serving floor if shuffle data available
        if intent.shuffle_write_gb and intent.shuffle_write_gb > 1000:
            serving = math.ceil(intent.shuffle_write_gb / (0.04 * target_h * 3600))
            n = max(n, serving)
        return max(10, int(n * multiplier))

    # Mode 2: User provided target_duration → estimate from throughput model
    if intent.target_duration_minutes is not None:
        n = _est_executors(intent.input_size_gb, cores, intent.target_duration_minutes, multiplier)
        # Boost for shuffle-heavy: if shuffle known and large, check serving floor
        if intent.shuffle_write_gb and intent.shuffle_write_gb > 1000:
            target_sec = intent.target_duration_minutes * 60
            serving = math.ceil(intent.shuffle_write_gb / (0.04 * target_sec))
            n = max(n, serving)
        return n

    # Mode 1: No input — generous default (dynamic alloc scales down unused)
    return DEFAULT_MAX_EXECUTORS[size]


def _max_partition_bytes(input_gb: float) -> str:
    if input_gb < 10: return "64m"
    if input_gb < 500: return "128m"
    if input_gb < 3000: return "256m"
    return "512m"


def _partitions(size_or_gb, computed: int) -> int:
    """Enforce minimum 1000 for S+ sizes, cap at 10000.
    AQE coalesces unused partitions automatically."""
    if isinstance(size_or_gb, str):
        is_xs = size_or_gb == "XS"
    else:
        is_xs = size_or_gb <= 5
    if is_xs:
        return max(20, min(computed, 10000))
    return max(1000, min(computed, 10000))


def _base_configs() -> Dict[str, str]:
    return {
        "spark.dynamicAllocation.enabled": "true",
        "spark.sql.optimizer.excludedRules": WGL_RULE,
    }


# ─── XS (Extra-Small / Micro) ────────────────────────────────────────────────

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
    return BucketResult("XS", "General", "small", configs,
        rationale="Micro job (describe, count, SCD2, catalog ops) — minimal resources")


# ─── Iceberg Maintenance ──────────────────────────────────────────────────────

def _iceberg_maintenance(intent: WorkloadIntent) -> BucketResult:
    num_files = intent.num_files or max(20, int(intent.input_size_gb * 1024 / 100))
    max_exec = min(100, max(5, math.ceil(num_files / 20)))
    is_sort = intent.workload_type == "iceberg_maintenance" and intent.shuffle_write_gb and intent.shuffle_write_gb > 0
    partitions = _partitions(intent.input_size_gb, max(200, int(num_files / 5)) if is_sort else 200)

    configs = {
        **_base_configs(),
        "spark.executor.cores": "4",
        "spark.executor.memory": "14G",
        "spark.driver.cores": "4",
        "spark.driver.memory": "14G",
        "spark.dynamicAllocation.maxExecutors": str(max_exec),
        "spark.sql.shuffle.partitions": str(partitions),
        "spark.sql.files.maxPartitionBytes": "512m",
        "spark.emr-serverless.executor.disk": "200G",
        "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
    }
    return BucketResult("M", "Iceberg-Maintenance", "small", configs,
        rationale=f"Iceberg maintenance (compaction/expire/rewrite) — scale by file count ({num_files} files)")


# ─── General (default sub-bucket for S/M/L/XL) ───────────────────────────────

def _general(size: str, intent: WorkloadIntent) -> BucketResult:
    n = _resolve_max_executors(size, intent, 4)
    w = _pick_worker(n)
    if w["cores"] == 8:
        n = math.ceil(n / 2)
    configs = {
        **_base_configs(),
        "spark.executor.cores": str(w["cores"]),
        "spark.executor.memory": w["mem"],
        "spark.driver.cores": "4" if n <= 50 else "8",
        "spark.driver.memory": "14G" if n <= 50 else "27G",
        "spark.dynamicAllocation.maxExecutors": str(n),
        "spark.sql.shuffle.partitions": str(_partitions(intent.input_size_gb, max(200, min(int(intent.input_size_gb * 2), 2 * n * w["cores"])))),
        "spark.sql.files.maxPartitionBytes": _max_partition_bytes(intent.input_size_gb),
        "spark.emr-serverless.executor.disk": "200G",
        "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
        "spark.network.timeout": "1200s",
    }
    return BucketResult(size, "General", w["label"], configs,
        rationale="Balanced defaults — suitable for most workloads")


# ─── Compute-Optimized ────────────────────────────────────────────────────────

def _compute_optimized(size: str, intent: WorkloadIntent) -> BucketResult:
    n = _resolve_max_executors(size, intent, 4)
    w = _pick_worker(n)
    if w["cores"] == 8:
        n = math.ceil(n / 2)
    shuffle_est = intent.input_size_gb * 0.1  # low shuffle for pure ETL
    parts = _partitions(intent.input_size_gb, min(int(shuffle_est * 1024 / 128), 2 * n * w["cores"]))
    configs = {
        **_base_configs(),
        "spark.executor.cores": str(w["cores"]),
        "spark.executor.memory": w["mem"],
        "spark.driver.cores": "4",
        "spark.driver.memory": "14G" if n <= 50 else "27G",
        "spark.dynamicAllocation.maxExecutors": str(n),
        "spark.sql.shuffle.partitions": str(parts),
        "spark.sql.files.maxPartitionBytes": _max_partition_bytes(intent.input_size_gb),
        "spark.emr-serverless.executor.disk": "200G",
        "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
        "spark.network.timeout": "1200s",
    }
    return BucketResult(size, "Compute-Optimized", w["label"], configs,
        rationale="CPU-bound ETL — maximize scan throughput, minimal shuffle overhead")


# ─── Memory-Optimized ─────────────────────────────────────────────────────────

def _memory_optimized(size: str, intent: WorkloadIntent) -> BucketResult:
    w = {"cores": 8, "mem": "54G", "label": "medium"}  # always Medium
    n = _resolve_max_executors(size, intent, 8)
    n = max(10, n)
    parts = _partitions(size, min(2000, int(intent.largest_table_gb * 8)))
    configs = {
        **_base_configs(),
        "spark.executor.cores": "8",
        "spark.executor.memory": "54G",
        "spark.driver.cores": "8",
        "spark.driver.memory": "54G",
        "spark.dynamicAllocation.maxExecutors": str(n),
        "spark.sql.shuffle.partitions": str(parts),
        "spark.sql.files.maxPartitionBytes": _max_partition_bytes(intent.input_size_gb),
        "spark.emr-serverless.executor.disk": "1000G",
        "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
        "spark.network.timeout": "1200s",
    }
    return BucketResult(size, "Memory-Optimized", "medium", configs,
        rationale=f"Many joins ({intent.num_joins}) — fat executors, more memory per task")


# ─── Shuffle-Optimized ────────────────────────────────────────────────────────

def _shuffle_optimized(size: str, intent: WorkloadIntent) -> BucketResult:
    shuffle_gb = intent.shuffle_write_gb or intent.input_size_gb * 0.4
    if intent.target_duration_minutes is not None:
        target_sec = intent.target_duration_minutes * 60
        serving_floor = math.ceil(shuffle_gb / (0.04 * target_sec))
        n = max(_est_executors(intent.input_size_gb, 4, intent.target_duration_minutes), serving_floor)
    else:
        n = DEFAULT_MAX_EXECUTORS[size]
    w = _pick_worker(n)
    if w["cores"] == 8:
        n = math.ceil(n / 2)
    parts = min(int(shuffle_gb * 1024 / 128), 2 * n * w["cores"])
    parts = _partitions(size, parts)
    configs = {
        **_base_configs(),
        "spark.executor.cores": str(w["cores"]),
        "spark.executor.memory": w["mem"],
        "spark.driver.cores": "4" if n <= 70 else "8",
        "spark.driver.memory": "27G" if n <= 70 else "54G",
        "spark.dynamicAllocation.maxExecutors": str(n),
        "spark.sql.shuffle.partitions": str(parts),
        "spark.sql.files.maxPartitionBytes": _max_partition_bytes(intent.input_size_gb),
        "spark.emr-serverless.executor.disk": "1000G",
        "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
        "spark.network.timeout": "1200s",
    }
    return BucketResult(size, "Shuffle-Optimized", w["label"], configs,
        rationale="Heavy shuffle — executor count driven by network serving ceiling")


# ─── IO-Optimized ─────────────────────────────────────────────────────────────

def _io_optimized(size: str, intent: WorkloadIntent) -> BucketResult:
    fan_out = intent.fan_out_factor or 100
    shuffle_gb = intent.input_size_gb * fan_out
    if intent.target_duration_minutes is not None:
        target_sec = intent.target_duration_minutes * 60 * 0.3
        io_floor = math.ceil((shuffle_gb * 1024) / (5 * max(1, target_sec)))
        n = max(10, min(200, io_floor))
    else:
        n = DEFAULT_MAX_EXECUTORS[size]
    # Always Small — maximize host count for disk throughput
    parts = min(int(shuffle_gb * 1024 / 128), 2 * n * 4)
    parts = _partitions(size, parts)
    configs = {
        **_base_configs(),
        "spark.executor.cores": "4",
        "spark.executor.memory": "27G",
        "spark.driver.cores": "4",
        "spark.driver.memory": "27G",
        "spark.dynamicAllocation.maxExecutors": str(n),
        "spark.sql.shuffle.partitions": str(parts),
        "spark.sql.files.maxPartitionBytes": "64m",
        "spark.emr-serverless.executor.disk": "1000G",
        "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
        "spark.network.timeout": "1200s",
    }
    return BucketResult(size, "IO-Optimized", "small", configs,
        rationale="Massive fan-out — many small workers for aggregate disk throughput")


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    # Print the full matrix
    print("\n" + "="*100)
    print("  EMR SERVERLESS CONFIG ADVISOR — SIZING MATRIX")
    print("="*100)

    examples = [
        ("XS/General",              WorkloadIntent(input_size_gb=0.01, workload_type="micro", target_duration_minutes=1)),
        ("S/Compute-Optimized",     WorkloadIntent(input_size_gb=50, workload_type="etl", target_duration_minutes=15)),
        ("S/General",               WorkloadIntent(input_size_gb=80, workload_type="aggregation", target_duration_minutes=20)),
        ("M/Compute-Optimized",     WorkloadIntent(input_size_gb=500, workload_type="etl", target_duration_minutes=30)),
        ("M/Shuffle-Optimized",     WorkloadIntent(input_size_gb=800, workload_type="aggregation", target_duration_minutes=45, shuffle_write_gb=1200)),
        ("L/Shuffle-Optimized",     WorkloadIntent(input_size_gb=3000, workload_type="aggregation", target_duration_minutes=60)),
        ("L/Memory-Optimized",      WorkloadIntent(input_size_gb=2500, workload_type="join_heavy", num_joins=40, largest_table_gb=1000, target_duration_minutes=45)),
        ("XL/Shuffle-Optimized",    WorkloadIntent(input_size_gb=5400, workload_type="join_heavy", num_joins=12, largest_table_gb=2000, target_duration_minutes=120, shuffle_write_gb=25000)),
        ("XL/Memory-Optimized",     WorkloadIntent(input_size_gb=18800, workload_type="join_heavy", num_joins=43, largest_table_gb=5000, target_duration_minutes=34, shj_count=43)),
        ("S/IO-Optimized",          WorkloadIntent(input_size_gb=2.4, workload_type="aggregation", target_duration_minutes=60, fan_out_factor=500, shuffle_ratio_pct=49500)),
        ("Iceberg Maintenance",     WorkloadIntent(input_size_gb=500, workload_type="iceberg_maintenance", is_compaction=True, num_files=10000)),
    ]

    for label, intent in examples:
        r = select_bucket(intent)
        max_e = r.configs.get("spark.dynamicAllocation.maxExecutors", "?")
        parts = r.configs.get("spark.sql.shuffle.partitions", "?")
        mpb = r.configs.get("spark.sql.files.maxPartitionBytes", "?")
        worker = f"{r.configs.get('spark.executor.cores','?')}c/{r.configs.get('spark.executor.memory','?')}"
        print(f"\n  ┌─ {r.label:<30} (expected: {label})")
        print(f"  │  {r.rationale}")
        print(f"  │  Worker: {worker}  maxExec: {max_e}  partitions: {parts}  maxPartBytes: {mpb}")
        print(f"  └─")

    print("\n" + "="*100)
    print("  DECISION TREE")
    print("="*100)
    print("""
  ┌─ SIZE CLASSIFICATION (by input volume + duration)
  │   XS: <5GB AND <5min     (describe, count, SCD2)
  │   S:  5-100GB
  │   M:  100GB-1TB
  │   L:  1-5TB
  │   XL: >5TB
  │
  ├─ SPECIAL CATEGORIES (bypass sub-bucket logic)
  │   Iceberg Maintenance: compaction, expire snapshots, rewrite manifests
  │
  └─ SUB-BUCKET (optimization axis, default=General)
      Memory-Optimized:   20+ joins OR 40+ SHJ
      IO-Optimized:       <10GB input + fan-out >100x
      Shuffle-Optimized:  shuffle >1TB OR ratio ≥30% OR large aggregation
      Compute-Optimized:  pure ETL, low shuffle
      General:            everything else (balanced defaults)
""")
