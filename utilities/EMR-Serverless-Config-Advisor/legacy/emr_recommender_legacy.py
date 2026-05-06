#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
EMR Serverless Recommender v16 (S3 JSON)
========================================
Read Spark event–log metrics from JSON files in S3 and produce
resource / configuration recommendations for EMR Serverless jobs.

Changes in v16:
- Changed target partition size from 2GB to 1GB
- Doubles partition count compared to v15
- Better parallelism for smaller executors

Author: Suthan Phillips <suthan@amazon.com>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, Iterable, Tuple

import boto3
import pandas as pd

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s %(levelname)-5s [%(name)s]  %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ds-recommender")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

REGION = "us-east-1"

WORKERS = {
    "Small": {"vcpu": 4, "memory": 27},
    "Medium": {"vcpu": 8, "memory": 54},
    "Large": {"vcpu": 16, "memory": 108},
}

DEFAULT_PARTITIONS = 1000
DEFAULT_COALESCE = 25

# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #

def _gb_to_gib(gb: float) -> float:
    return round(gb * 0.931323, 2)


def _calculate_shuffle_ratio(input_gb, read_gb, write_gb) -> float:
    if input_gb == 0:
        return 0.0
    return round((read_gb + write_gb) / input_gb * 100.0, 2)


def _select_worker_type(input_gb: float, shuffle_ratio: float) -> Tuple[str, Dict]:
    """
    V10: Dynamic worker selection based on data volume and shuffle intensity.
    
    Logic:
    - Large (16 vCPU, 108 GB): > 2 TB OR shuffle > 70%
    - Medium (8 vCPU, 54 GB): 500 GB - 2 TB OR shuffle 40-70%
    - Small (4 vCPU, 27 GB): < 500 GB AND shuffle < 40%
    """
    if input_gb > 2048 or shuffle_ratio > 70:
        return "Large", WORKERS["Large"]
    elif input_gb > 500 or shuffle_ratio > 40:
        return "Medium", WORKERS["Medium"]
    else:
        return "Small", WORKERS["Small"]


def _compute_exec_limits(input_gb: float, vcpu: int, partitions: int = 0, 
                        mem_pct: float = 60.0, cpu_pct: float = 50.0, 
                        idle_pct: float = 50.0, spill_gb: float = 0.0,
                        mode: str = "cost") -> Tuple[int, int]:
    """
    Calculate executors using resource pressure.
    
    mode: "cost" = conservative (existing logic)
          "performance" = aggressive for high memory pressure
    """
    # Base calculations
    req_input = max(1, int(input_gb / 100))
    req_part = max(1, int((partitions / vcpu) / 3)) if partitions > 0 else 0
    base_req = max(req_input, req_part)
    
    if mode == "performance":
        # Performance mode: aggressive scaling for high memory
        if mem_pct > 75:
            factor = 1.0 + ((mem_pct - 75) / 25) * 0.5  # 75-100% → 1.0-1.5x
        else:
            # Standard pressure calculation for lower memory
            mem_pressure = mem_pct * 0.4
            cpu_pressure = cpu_pct * 0.4
            spill_ratio = (spill_gb / max(input_gb, 1)) * 100
            spill_pressure = min(20, spill_ratio * 0.2)
            pressure = max(0, min(100, mem_pressure + cpu_pressure + spill_pressure))
            factor = 0.5 + (pressure / 100) * 1.0
    else:
        # Cost mode: existing conservative logic
        mem_pressure = mem_pct * 0.4
        cpu_pressure = cpu_pct * 0.4
        spill_ratio = (spill_gb / max(input_gb, 1)) * 100
        spill_pressure = min(20, spill_ratio * 0.2)
        pressure = max(0, min(100, mem_pressure + cpu_pressure + spill_pressure))
        factor = 0.5 + (pressure / 100) * 1.0
    
    max_exec = max(2, int(base_req * factor))
    min_exec = max(1, max_exec // 2)
    
    return max_exec, min_exec


def _auto_tune_shuffles(worker_type: str, worker_vcpu: int, worker_memory_gb: int, 
                       shuffle_bytes: float, max_executors: int) -> Tuple[int, int]:
    """
    Calculate shuffle partitions based on worker profile and shuffle data size.
    
    Logic:
    1. Calculate effective executor RAM (after 10% overhead)
    2. Target partition size = 1 GB
    3. Compute partitions from shuffle bytes
    4. Round to even number
    
    Note: No minimum parallelism constraint since dynamic allocation
    will scale executors based on actual partition count.
    """
    # Step 1: Effective executor RAM after 10% overhead
    overhead_pct = 0.10
    effective_mem_gb = worker_memory_gb * (1 - overhead_pct)
    effective_mem_mib = effective_mem_gb * 1024
    
    # Step 2: Target partition size = 1 GB for all worker classes
    target_mib = 1024  # 1 GB target partition size
    
    # Step 3: Compute partitions from shuffle bytes
    if shuffle_bytes > 0:
        partitions = int((shuffle_bytes / (target_mib * 1024 * 1024)) + 0.5)  # ceil
    else:
        partitions = 200
    
    # Step 4: Round to even number
    if partitions % 2 != 0:
        partitions += 1
    
    return partitions, target_mib


def _get_timeout_configs(input_gb: float, duration_hours: float) -> Dict[str, str]:
    base_timeout = 600
    data_factor = int(input_gb / 1000) * 60
    
    if duration_hours and not (duration_hours != duration_hours):
        duration_factor = int(duration_hours) * 120
    else:
        duration_factor = 0
    
    shuffle_timeout = min(max(base_timeout + data_factor + duration_factor, 600), 1800)
    network_timeout = shuffle_timeout * 2
    
    return {
        "spark.network.timeout": f"{network_timeout}s",
        "spark.shuffle.io.connectionTimeout": f"{shuffle_timeout}s",
    }


def _get_s3_retry_configs(input_gb: float) -> Dict[str, str]:
    if input_gb < 100:
        retries = "5"
    elif input_gb < 1000:
        retries = "10"
    else:
        retries = "15"
    
    return {
        "spark.hadoop.fs.s3a.retry.limit": retries,
        "spark.hadoop.fs.s3a.connection.ssl.enabled": "true",
    }


def _get_iceberg_configs() -> Dict[str, str]:
    return {
        "spark.sql.catalog.spark_catalog": "org.apache.iceberg.spark.SparkSessionCatalog",
        "spark.sql.catalog.spark_catalog.type": "hive",
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
    }


def _max_partition_bytes(input_gb: float) -> str:
    if input_gb >= 1024:
        return "512m"
    return "128m"


def _calculate_executor_disk(shuffle_write_gb: float, disk_spill_gb: float, 
                             memory_spill_gb: float, max_executors: int) -> str:
    """
    Calculate executor disk size (500GB - 2000GB) based on shuffle and spill metrics.
    """
    # Estimate disk needed per executor
    total_shuffle_per_exec = shuffle_write_gb / max(max_executors, 1)
    total_spill_per_exec = (disk_spill_gb + memory_spill_gb) / max(max_executors, 1)
    
    # Add 50% buffer for temporary files
    estimated_gb = (total_shuffle_per_exec + total_spill_per_exec) * 1.5
    
    # Apply min/max bounds
    disk_gb = max(500, min(2000, int(estimated_gb)))
    
    return f"{disk_gb}G"


# --------------------------------------------------------------------------- #
# Core recommender class
# --------------------------------------------------------------------------- #

class EmrRecommender:
    def __init__(self, s3_paths: list[str], region: str = REGION) -> None:
        self.s3_paths = [p.rstrip('/') for p in s3_paths]
        self.region = region
        self.s3_client = boto3.client('s3', region_name=region)
        log.info("Initialized S3 reader for %d path(s)", len(s3_paths))

    def _load_metrics_from_s3(self, limit: int = 100) -> pd.DataFrame:
        """Load JSON files from S3 and extract metrics."""
        all_data = []
        
        for s3_path in self.s3_paths:
            bucket, prefix = s3_path.replace('s3://', '').split('/', 1)
            prefix = prefix.rstrip('/') + '/task_stage_summary/'
            
            log.info("Listing objects in s3://%s/%s", bucket, prefix)
            paginator = self.s3_client.get_paginator('list_objects_v2')
            
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                if 'Contents' not in page:
                    continue
                    
                for obj in page['Contents']:
                    key = obj['Key']
                    if not key.endswith('.json'):
                        continue
                    
                    try:
                        log.debug("Reading s3://%s/%s", bucket, key)
                        response = self.s3_client.get_object(Bucket=bucket, Key=key)
                        content = response['Body'].read().decode('utf-8')
                        data = json.loads(content)
                        
                        # Extract executor utilization metrics
                        exec_summary = data.get('executor_summary', {})
                        spill_summary = data.get('spill_summary', {})
                        
                        # Flatten the structure
                        flattened = {
                            'application_id': data.get('application_id'),
                            'application_name': data.get('application_info', {}).get('application_name'),
                            'total_run_duration_hours': data.get('total_run_duration_hours'),
                            'io_total_input_gb': data.get('io_summary', {}).get('application_level', {}).get('total_input_gb', 0),
                        'io_total_shuffle_read_gb': data.get('io_summary', {}).get('application_level', {}).get('total_shuffle_read_gb', 0),
                        'io_total_shuffle_write_gb': data.get('io_summary', {}).get('application_level', {}).get('total_shuffle_write_gb', 0),
                        'avg_memory_utilization_percent': exec_summary.get('avg_memory_utilization_percent', 60.0),
                        'avg_cpu_utilization_percent': exec_summary.get('avg_cpu_utilization_percent', 50.0),
                        'idle_core_percentage': exec_summary.get('idle_core_percentage', 50.0),
                        'total_memory_spilled_gb': spill_summary.get('total_memory_spilled_gb', 0.0),
                        'total_disk_spilled_gb': spill_summary.get('total_disk_spilled_gb', 0.0),
                    }
                        all_data.append(flattened)
                    except Exception as e:
                        log.debug("Skipping %s: %s", key, e)
                        continue
        
        log.info("Loaded %d JSON files from S3", len(all_data))
        
        df = pd.DataFrame(all_data)
        
        # Filter and sort
        df = df[df['io_total_input_gb'] > 0]
        df = df.sort_values('io_total_input_gb', ascending=False).head(limit)
        
        log.info("Filtered to %d records with input > 0 GB", len(df))
        return df

    def generate(self, limit: int = 100) -> list[Dict]:
        df = self._load_metrics_from_s3(limit)

        recommendations = []

        for _, row in df.iterrows():
            app_id = row.get('application_id', 'N/A')
            name = row.get('application_name', 'N/A')
            duration = float(row.get('total_run_duration_hours', 0) or 0)
            i_in_gb = float(row.get('io_total_input_gb', 0) or 0)
            s_in_gb = float(row.get('io_total_shuffle_read_gb', 0) or 0)
            s_out_gb = float(row.get('io_total_shuffle_write_gb', 0) or 0)
            
            # Extract utilization metrics
            mem_pct = float(row.get('avg_memory_utilization_percent', 60.0) or 60.0)
            cpu_pct = float(row.get('avg_cpu_utilization_percent', 50.0) or 50.0)
            idle_pct = float(row.get('idle_core_percentage', 50.0) or 50.0)
            spill_gb = float(row.get('total_memory_spilled_gb', 0.0) or 0.0)
            disk_spill_gb = float(row.get('total_disk_spilled_gb', 0.0) or 0.0)

            sh_ratio = _calculate_shuffle_ratio(i_in_gb, s_in_gb, s_out_gb)

            worker_type, worker_cfg = _select_worker_type(i_in_gb, sh_ratio)
            
            # Calculate shuffle partitions first
            shuffle_data_gb = max(s_in_gb, s_out_gb)
            shuffle_bytes = shuffle_data_gb * 1024 * 1024 * 1024
            
            # Initial executor limits (will be recalculated after partitions)
            max_exec_initial, min_exec = _compute_exec_limits(
                i_in_gb, worker_cfg["vcpu"], 0, mem_pct, cpu_pct, idle_pct, spill_gb
            )
            
            # Calculate shuffle partitions
            sp, target_mib = _auto_tune_shuffles(
                worker_type, 
                worker_cfg["vcpu"], 
                worker_cfg["memory"],
                shuffle_bytes,
                max_exec_initial
            )
            
            # Recalculate max executors considering partition count
            max_exec, min_exec = _compute_exec_limits(
                i_in_gb, worker_cfg["vcpu"], sp, mem_pct, cpu_pct, idle_pct, spill_gb
            )
            
            # Calculate dynamic executor disk size
            executor_disk = _calculate_executor_disk(s_out_gb, disk_spill_gb, spill_gb, max_exec)

            spark_cfg = {
                "spark.driver.cores": "8",
                "spark.driver.memory": "54G",
                "spark.executor.cores": str(worker_cfg["vcpu"]),
                "spark.executor.memory": f"{worker_cfg['memory']}g",
                "spark.dynamicAllocation.enabled": "true",
                "spark.sql.adaptive.enabled": "true",
                "spark.sql.files.maxPartitionBytes": _max_partition_bytes(i_in_gb),
                "spark.emr-serverless.executor.disk": executor_disk,
                "spark.emr-serverless.executor.disk.type": "shuffle_optimized",
            }
            
            spark_cfg.update(_get_timeout_configs(i_in_gb, duration))
            spark_cfg.update(_get_s3_retry_configs(i_in_gb))
            spark_cfg.update(_get_iceberg_configs())

            spark_cfg["spark.sql.shuffle.partitions"] = str(sp)

            if sh_ratio > 30:
                spark_cfg.update(
                    {
                        "spark.shuffle.compress": "true",
                        "spark.shuffle.spill.compress": "true",
                    }
                )

            spark_cfg["spark.dynamicAllocation.maxExecutors"] = str(max_exec)
            spark_cfg["spark.dynamicAllocation.minExecutors"] = str(min_exec)
            spark_cfg["spark.dynamicAllocation.initialExecutors"] = str(min_exec)

            recommendation = {
                "application_id": app_id,
                "application_name": name,
                "metrics": {
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
                },
                "worker": {
                    "type": worker_type,
                    "vcpu": worker_cfg["vcpu"],
                    "memory_gb": worker_cfg["memory"],
                    "max_executors": max_exec,
                    "min_executors": min_exec,
                    "total_vcpu_capacity": max_exec * worker_cfg["vcpu"],
                    "total_memory_capacity": max_exec * worker_cfg["memory"],
                },
                "spark_configs": spark_cfg,
                "shuffle_tuned": {
                    "partitions": sp,
                    "target_partition_size_mib": target_mib,
                    "auto_tuned": True,
                },
            }

            recommendations.append(recommendation)

        log.info("Generated %d recommendation(s)", len(recommendations))
        return recommendations

    def persist(self, recs: Iterable[Dict], out_file: Path) -> None:
        out_file = Path(out_file)
        out_file.write_text(json.dumps(list(recs), indent=2))
        log.info("JSON written to %s", out_file)

        csv = out_file.with_suffix(".csv")
        summary = {
            "application_id": [],
            "application_name": [],
            "input_gb": [],
            "shuffle_ratio": [],
            "worker_type": [],
            "vcpu": [],
            "memory_gb": [],
            "max_executors": [],
            "shuffle_partitions": [],
            "auto_tuned": [],
            "total_vcpu": [],
        }

        for r in recs:
            w = r["worker"]
            ct = r["shuffle_tuned"]
            summary["application_id"].append(r["application_id"])
            summary["application_name"].append(r["application_name"])
            summary["input_gb"].append(r["metrics"]["input_gb"])
            summary["shuffle_ratio"].append(r["metrics"]["shuffle_ratio_percent"])
            summary["worker_type"].append(w["type"])
            summary["vcpu"].append(w["vcpu"])
            summary["memory_gb"].append(w["memory_gb"])
            summary["max_executors"].append(w["max_executors"])
            summary["shuffle_partitions"].append(ct["partitions"])
            summary["auto_tuned"].append(ct["auto_tuned"])
            summary["total_vcpu"].append(w["total_vcpu_capacity"])
        pd.DataFrame(summary).to_csv(csv, index=False)
        log.info("CSV summary written to %s", csv)

        self._print_tui(recs)

    def _print_tui(self, recs: Iterable[Dict], n: int = 10) -> None:
        header = (
            "No | App | InGB | ShRatio% | Worker | Exec | Partitions | Auto"
        )
        print("\n" + "=" * 80)
        print(f"{header}")
        print("=" * 80)

        for i, r in enumerate(recs[:n], 1):
            w = r["worker"]
            c = r["shuffle_tuned"]
            print(
                f"{i:>2} | "
                f"{r['application_name'][:35]:35} | "
                f"{r['metrics']['input_gb']:>5} | "
                f"{r['metrics']['shuffle_ratio_percent']:>7.1f} | "
                f"{w['type']:<6} | "
                f"{w['max_executors']:>5} | "
                f"{c['partitions']:<10} | "
                f"{'✓' if c['auto_tuned'] else ''}"
            )
        print("=" * 80, flush=True)


# --------------------------------------------------------------------------- #
# Command‑line frontend
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        "emr-serverless-recommender-s3",
        description="Generate EMR Serverless configuration recommendations from S3 JSON files.",
    )
    parser.add_argument(
        "--s3-path",
        required=True,
        nargs='+',
        help="S3 path(s) containing JSON files (e.g., s3://bucket/prefix/). Can specify multiple paths.",
    )
    parser.add_argument(
        "--region",
        default=REGION,
        help="AWS region",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="How many of the largest jobs to analyze",
    )
    parser.add_argument(
        "--output",
        default="emr_serverless_recs_v13.json",
        help="Path to write the JSON + CSV summary",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Turn on verbose logging"
    )
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    recommender = EmrRecommender(args.s3_path, args.region)
    recs = recommender.generate(args.limit)
    recommender.persist(recs, Path(args.output))

    print("\n✅  Done. Check the output JSON/CSV.\n")


if __name__ == "__main__":
    main(sys.argv[1:])
