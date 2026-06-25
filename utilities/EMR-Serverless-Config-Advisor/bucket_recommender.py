#!/usr/bin/env python3
"""
EMR Serverless Bucket Recommender — Optimal Spark configs for new jobs without event logs.

Assigns a pre-configured "bucket" of Spark settings based on:
  - T-shirt size (XS/S/M/L/XL) — driven by input data volume
  - Sub-category (General/Compute/Shuffle/Memory/IO/Iceberg) — driven by workload pattern

Usage:
  # First run — just pick your size:
  python3 bucket_recommender.py --size M

  # With sub-category:
  python3 bucket_recommender.py --size L --sub-category Shuffle-Optimized

  # As spark-submit params:
  python3 bucket_recommender.py --size M --format spark-submit

  # After first run, use the full Config Advisor with event log for precise tuning:
  python3 emr_recommender.py --input-path s3://your-bucket/event-logs/application_id/
"""
import argparse
import json
import math
from typing import Optional

# EMR 7.x window optimization bug — exclude unconditionally (zero cost if no windows)
WGL_RULE = "org.apache.spark.sql.catalyst.optimizer.InferWindowGroupLimit"

# Generous defaults when user provides no sizing input.
# Dynamic allocation scales down unused executors — no cost penalty.
DEFAULT_MAX_EXECUTORS = {"XS": 3, "S": 50, "M": 100, "L": 200, "XL": 500}

SIZES = ["XS", "S", "M", "L", "XL"]
SUB_CATEGORIES = ["General", "Compute-Optimized", "Shuffle-Optimized", "Memory-Optimized", "IO-Optimized", "Iceberg-Maintenance"]


def recommend(
    size: str,
    sub_category: str = "General",
    target_duration_minutes: Optional[int] = None,
    input_size_gb: Optional[float] = None,
    shuffle_write_gb: Optional[float] = None,
    task_hours: Optional[float] = None,
    fan_out_factor: Optional[float] = None,
    num_files: Optional[int] = None,
) -> dict:
    """Generate Spark configs for given size + sub-category."""
    size = size.upper()
    assert size in SIZES, f"Size must be one of {SIZES}"

    cores, mem, max_exec = _resolve_worker_and_executors(
        size, sub_category, target_duration_minutes, input_size_gb, shuffle_write_gb, task_hours
    )
    partitions = _resolve_partitions(size, input_size_gb, shuffle_write_gb, max_exec, cores)
    mpb = _max_partition_bytes(size, input_size_gb or 100)

    # Base configs (all buckets)
    configs = {
        "spark.executor.cores": str(cores),
        "spark.executor.memory": mem,
        "spark.driver.cores": str(min(cores, 8)),
        "spark.driver.memory": mem if cores >= 8 else ("27G" if max_exec > 50 else "14G"),
        "spark.dynamicAllocation.enabled": "true",
        "spark.dynamicAllocation.maxExecutors": str(max_exec),
        "spark.dynamicAllocation.minExecutors": str(max(1 if size == "XS" else 3, int(max_exec * 0.3))),
        "spark.sql.shuffle.partitions": str(partitions),
        "spark.sql.files.maxPartitionBytes": mpb,
        "spark.sql.adaptive.enabled": "true",
        "spark.sql.optimizer.excludedRules": WGL_RULE,
    }

    # Sub-category specific overrides
    if sub_category == "Shuffle-Optimized":
        configs.update({
            "spark.shuffle.compress": "true",
            "spark.shuffle.spill.compress": "true",
            "spark.emr-serverless.executor.disk": "1000G",
            "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
            "spark.sql.adaptive.advisoryPartitionSizeInBytes": "256m",
            "spark.network.timeout": "1200s",
        })
    elif sub_category == "Memory-Optimized":
        configs.update({
            "spark.sql.autoBroadcastJoinThreshold": "-1",
            "spark.sql.join.forceApplyShuffledHashJoin": "false",
            "spark.sql.adaptive.advisoryPartitionSizeInBytes": "64m",
            "spark.emr-serverless.executor.disk": "1000G",
            "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
            "spark.memory.fraction": "0.7",
        })
    elif sub_category == "IO-Optimized":
        configs.update({
            "spark.shuffle.compress": "true",
            "spark.shuffle.spill.compress": "true",
            "spark.emr-serverless.executor.disk": "1000G",
            "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
            "spark.network.timeout": "1200s",
        })
    elif sub_category == "Iceberg-Maintenance":
        configs.update({
            "spark.sql.files.maxPartitionBytes": "512m",
        })
        # Override executor sizing for compaction
        if num_files:
            configs["spark.dynamicAllocation.maxExecutors"] = str(min(100, max(5, math.ceil(num_files / 20))))

    # XS overrides (micro jobs)
    if size == "XS":
        configs["spark.dynamicAllocation.initialExecutors"] = "1"

    return {
        "size": size,
        "sub_category": sub_category,
        "spark_configs": configs,
        "spark_submit_params": " ".join(f"--conf {k}={v}" for k, v in configs.items()),
    }


def _resolve_worker_and_executors(size, sub_category, target_min, input_gb, shuffle_gb, task_hours):
    """Determine worker type and maxExecutors based on available inputs."""
    if size == "XS":
        return 1, "2G", 3

    # Memory-Optimized always uses Medium (8c/54G)
    if sub_category == "Memory-Optimized":
        cores, mem = 8, "54G"
    elif size in ("XL",):
        cores, mem = 8, "54G"
    else:
        cores, mem = 4, "27G"

    # Mode 3: Event log (most precise)
    if task_hours and task_hours > 0:
        target_h = (target_min / 60.0) if target_min else 1.0
        max_exec = math.ceil(task_hours / (target_h * 0.70 * cores))
        if shuffle_gb and shuffle_gb > 1000 and target_min:
            serving = math.ceil(shuffle_gb / (0.04 * target_min * 60))
            max_exec = max(max_exec, serving)
        return cores, mem, max(10, max_exec)

    # Mode 2: Proxy (target duration provided)
    if target_min and input_gb:
        throughput = 0.5 * cores
        work_min = input_gb / max(0.1, throughput)
        max_exec = math.ceil(work_min / (target_min * 0.50))
        if shuffle_gb and shuffle_gb > 1000:
            serving = math.ceil(shuffle_gb / (0.04 * target_min * 60))
            max_exec = max(max_exec, serving)
        return cores, mem, max(10, max_exec)

    # Mode 1: Generous default
    max_exec = DEFAULT_MAX_EXECUTORS[size]
    # Promote to Medium if default exceeds 70
    if max_exec > 70 and cores == 4:
        cores, mem = 8, "54G"
        max_exec = math.ceil(max_exec / 2)
    return cores, mem, max_exec


def _resolve_partitions(size, input_gb, shuffle_gb, max_exec, cores):
    """Minimum 1000 for S+. AQE coalesces unused partitions."""
    if size == "XS":
        return 20
    shuf = shuffle_gb or (input_gb or 100) * 0.3
    computed = min(int(shuf * 1024 / 128), 2 * max_exec * cores)
    return max(1000, computed)


def _max_partition_bytes(size, input_gb):
    if size == "XS":
        return "32m"
    if input_gb < 10:
        return "64m"
    if input_gb < 500:
        return "128m"
    if input_gb < 3000:
        return "256m"
    return "512m"


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="EMR Serverless Bucket Recommender")
    p.add_argument("--size", required=True, choices=SIZES, help="T-shirt size (XS/S/M/L/XL)")
    p.add_argument("--sub-category", default="General", choices=SUB_CATEGORIES, help="Optimization axis (default: General)")
    p.add_argument("--input-size-gb", type=float, help="Input data size in GB (improves maxPartitionBytes selection)")
    p.add_argument("--num-files", type=int, help="Number of files to compact (for Iceberg-Maintenance)")
    p.add_argument("--format", choices=["json", "spark-submit", "table"], default="table")
    args = p.parse_args()

    result = recommend(
        size=args.size,
        sub_category=args.sub_category,
        input_size_gb=args.input_size_gb,
        num_files=args.num_files,
    )

    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif args.format == "spark-submit":
        print(result["spark_submit_params"])
    else:
        print(f"\n  Bucket: {result['size']}/{result['sub_category']}\n")
        for k, v in result["spark_configs"].items():
            print(f"  {k:<55} = {v}")
        print()
