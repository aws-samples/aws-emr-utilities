#!/usr/bin/env python3
"""
EMR Serverless Application Settings Consolidator
=================================================
Rolls up per-job Fine Tuner recommendations into recommended EMR Serverless
*application* settings: maximumCapacity, (optional) pre-initialized capacity,
and consistency checks across the job mix.

Per-job recommendations size a single StartJobRun. An EMR Serverless
*application* has its own settings that span every job that runs on it:
  - maximumCapacity: the cpu / memory / disk ceiling the app may scale to
  - initialCapacity (pre-initialized "warm pool"): workers kept ready to cut
    cold-start latency (billed while idle)
  - architecture / releaseLabel: must be consistent across jobs

This tool consumes the Fine Tuner's recommendation output (a JSON list of
per-job recommendations, each with a `worker` block and `spark_configs`) and
produces application-level settings.

Concurrency matters for the capacity ceiling:
  --concurrency sequential      app ceiling = the single largest job (jobs never overlap)
  --concurrency peak-concurrent app ceiling = sum of all jobs (all may run at once)
  --concurrency N               app ceiling = sum of the N largest jobs (at most N overlap)

Usage:
  # From a Fine Tuner recommendations file (list of per-job recs):
  python3 emr_s_app_consolidator.py --input recommendations_cost.json

  # From a directory of per-job recommendation/job-config JSONs:
  python3 emr_s_app_consolidator.py --input /tmp/recs/ --concurrency peak-concurrent

  # Also recommend a pre-initialized warm pool and add 25% headroom:
  python3 emr_s_app_consolidator.py --input recs.json --pre-init --headroom 25
"""
import argparse
import glob
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional

# EMR Serverless default per-worker disk when unspecified (GB)
DEFAULT_DISK_GB = 20
# Driver disk footprint assumed when driver disk is unspecified (GB)
DEFAULT_DRIVER_DISK_GB = 20


@dataclass
class JobDemand:
    """Peak resource footprint of a single job at full scale-up."""
    name: str
    exec_cores: int
    exec_mem_gb: float
    max_executors: int
    min_executors: int
    driver_cores: int
    driver_mem_gb: float
    disk_per_worker_gb: float
    arch: Optional[str] = None
    release: Optional[str] = None

    @property
    def peak_cpu(self) -> int:
        # executors + 1 driver
        return self.max_executors * self.exec_cores + self.driver_cores

    @property
    def peak_mem_gb(self) -> float:
        return self.max_executors * self.exec_mem_gb + self.driver_mem_gb

    @property
    def peak_disk_gb(self) -> float:
        return self.max_executors * self.disk_per_worker_gb + DEFAULT_DRIVER_DISK_GB

    @property
    def worker_shape(self) -> str:
        return f"{self.exec_cores}vCPU/{self.exec_mem_gb:g}GB"


# ─── Parsing ──────────────────────────────────────────────────────────────────

def _parse_mem_gb(val) -> float:
    """Parse a Spark memory string like '54G', '8g', '512m' into GB."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().lower()
    m = re.match(r"^([\d.]+)\s*([gmk]?)b?$", s)
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = m.group(2)
    if unit == "g":
        return num
    if unit == "m":
        return num / 1024.0
    if unit == "k":
        return num / (1024.0 * 1024.0)
    return num  # assume GB


def _parse_disk_gb(val) -> float:
    if val is None:
        return DEFAULT_DISK_GB
    s = str(val).strip().upper().rstrip("B")
    m = re.match(r"^([\d.]+)\s*G?$", s)
    return float(m.group(1)) if m else DEFAULT_DISK_GB


def _int(val, default=0) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def job_demand_from_rec(rec: dict) -> Optional[JobDemand]:
    """Extract a JobDemand from either a raw Fine Tuner recommendation
    (has a `worker` block) or a job-config document (has configuration.spark_conf)."""
    # Normalize: job-config docs nest under 'configuration'
    if "configuration" in rec and "spark_conf" in rec.get("configuration", {}):
        sc = rec["configuration"]["spark_conf"]
        name = rec.get("job_name") or rec.get("ams_app_name") or "job"
        cpp = rec["configuration"].get("compute_platform_properties", {}) or {}
        arch = "ARM64" if cpp.get("graviton_enabled") else None
        exec_cores = _int(sc.get("spark.executor.cores"), 4)
        exec_mem = _parse_mem_gb(sc.get("spark.executor.memory"))
        max_exec = _int(sc.get("spark.dynamicAllocation.maxExecutors"), 0)
        min_exec = _int(sc.get("spark.dynamicAllocation.minExecutors"), 0)
        drv_cores = _int(sc.get("spark.driver.cores"), exec_cores)
        drv_mem = _parse_mem_gb(sc.get("spark.driver.memory"))
        disk = _parse_disk_gb(sc.get("spark.emr-serverless.executor.disk"))
        release = None
    else:
        w = rec.get("worker", {})
        sc = rec.get("spark_configs", {}) or {}
        name = rec.get("application_name") or rec.get("job_id") or "job"
        arch = rec.get("architecture")
        exec_cores = _int(w.get("vcpu"), _int(sc.get("spark.executor.cores"), 4))
        exec_mem = float(w.get("memory_gb") or _parse_mem_gb(sc.get("spark.executor.memory")))
        max_exec = _int(w.get("max_executors"), _int(sc.get("spark.dynamicAllocation.maxExecutors"), 0))
        min_exec = _int(w.get("min_executors"), _int(sc.get("spark.dynamicAllocation.minExecutors"), 0))
        drv_cores = _int(sc.get("spark.driver.cores"), exec_cores)
        drv_mem = _parse_mem_gb(sc.get("spark.driver.memory")) or exec_mem
        disk = _parse_disk_gb(sc.get("spark.emr-serverless.executor.disk"))
        release = rec.get("release_label")

    if max_exec <= 0 or exec_cores <= 0:
        return None
    return JobDemand(name=name, exec_cores=exec_cores, exec_mem_gb=exec_mem,
                     max_executors=max_exec, min_executors=min_exec,
                     driver_cores=drv_cores, driver_mem_gb=drv_mem,
                     disk_per_worker_gb=disk, arch=arch, release=release)


def load_recommendations(path: str) -> List[dict]:
    """Load recs from a single JSON (list or object) or a directory of JSONs."""
    files = []
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.json")))
    else:
        files = [path]
    recs = []
    for f in files:
        with open(f) as fh:
            data = json.load(fh)
        if isinstance(data, list):
            recs.extend(data)
        else:
            recs.append(data)
    return recs


# ─── Consolidation ──────────────────────────────────────────────────────────

def _round_cpu(vcpu: float) -> int:
    """EMR Serverless maximumCapacity cpu is a vCPU count; round up to a tidy number."""
    v = int(math.ceil(vcpu))
    # Round up to a multiple of 4 for a clean ceiling
    return int(math.ceil(v / 4.0) * 4)


def consolidate(jobs: List[JobDemand], concurrency: str, headroom_pct: float,
                mode_label: str) -> dict:
    if not jobs:
        raise ValueError("No valid job recommendations to consolidate")

    factor = 1.0 + headroom_pct / 100.0

    # Determine the capacity basis
    if concurrency == "sequential":
        base_cpu = max(j.peak_cpu for j in jobs)
        base_mem = max(j.peak_mem_gb for j in jobs)
        base_disk = max(j.peak_disk_gb for j in jobs)
        basis = "largest single job (jobs never overlap)"
    elif concurrency == "peak-concurrent":
        base_cpu = sum(j.peak_cpu for j in jobs)
        base_mem = sum(j.peak_mem_gb for j in jobs)
        base_disk = sum(j.peak_disk_gb for j in jobs)
        basis = "sum of all jobs (all may run simultaneously)"
    else:  # integer N — sum of the N largest
        n = int(concurrency)
        top_cpu = sorted((j.peak_cpu for j in jobs), reverse=True)[:n]
        top_mem = sorted((j.peak_mem_gb for j in jobs), reverse=True)[:n]
        top_disk = sorted((j.peak_disk_gb for j in jobs), reverse=True)[:n]
        base_cpu, base_mem, base_disk = sum(top_cpu), sum(top_mem), sum(top_disk)
        basis = f"sum of the {n} largest jobs (at most {n} overlap)"

    max_capacity = {
        "cpu": f"{_round_cpu(base_cpu * factor)} vCPU",
        "memory": f"{int(math.ceil(base_mem * factor))} GB",
        "disk": f"{int(math.ceil(base_disk * factor))} GB",
    }

    # Dominant worker shape (for pre-init / worker specs)
    shape_counts = Counter(j.worker_shape for j in jobs)
    dominant_shape, dominant_n = shape_counts.most_common(1)[0]
    dom_job = next(j for j in jobs if j.worker_shape == dominant_shape)

    # Pre-initialized capacity suggestion: dominant worker shape, count = median min_executors
    mins = sorted(j.min_executors for j in jobs if j.min_executors > 0)
    if mins:
        median_min = mins[len(mins) // 2]
    else:
        median_min = 0
    pre_init = {
        "worker_type": dom_job.worker_shape,
        "driver_count": 1,
        "executor_count": median_min if median_min > 0 else "n/a (jobs start from 0)",
        "note": ("Warm pool cuts cold-start latency but is billed while idle. "
                 "Size to your steady-state baseline, not peak."),
    }

    # Consistency checks
    archs = sorted({j.arch for j in jobs if j.arch})
    releases = sorted({j.release for j in jobs if j.release})
    warnings = []
    if len(archs) > 1:
        warnings.append(f"Jobs span multiple architectures ({', '.join(archs)}); an application is single-architecture — split or standardize.")
    if len(releases) > 1:
        warnings.append(f"Jobs span multiple release labels ({', '.join(releases)}); an application is single-release — standardize.")
    if len(shape_counts) > 3:
        warnings.append(f"{len(shape_counts)} distinct worker shapes across jobs — a pre-initialized pool only warms one shape; the rest cold-start.")

    return {
        "job_count": len(jobs),
        "capacity_basis": basis,
        "headroom_percent": headroom_pct,
        "maximumCapacity": max_capacity,
        "preInitializedCapacity": pre_init,
        "dominant_worker_shape": {"shape": dominant_shape, "jobs_using_it": dominant_n, "of": len(jobs)},
        "consistency_warnings": warnings,
        "per_job_peak_demand": [
            {"job": j.name, "cpu_vcpu": j.peak_cpu, "memory_gb": round(j.peak_mem_gb, 1),
             "disk_gb": round(j.peak_disk_gb, 1), "worker": j.worker_shape,
             "max_executors": j.max_executors}
            for j in sorted(jobs, key=lambda x: x.peak_cpu, reverse=True)
        ],
    }


# ─── Output ───────────────────────────────────────────────────────────────────

def print_report(result: dict):
    print(f"\n{'='*72}")
    print(f"  EMR Serverless — Recommended Application Settings")
    print(f"{'='*72}")
    print(f"  Consolidated from {result['job_count']} job recommendation(s)")
    print(f"  Capacity basis: {result['capacity_basis']}")
    print(f"  Headroom applied: {result['headroom_percent']:.0f}%")
    print()
    mc = result["maximumCapacity"]
    print(f"  maximumCapacity:")
    print(f"    cpu    = {mc['cpu']}")
    print(f"    memory = {mc['memory']}")
    print(f"    disk   = {mc['disk']}")
    print()
    pi = result["preInitializedCapacity"]
    print(f"  Pre-initialized capacity (optional warm pool):")
    print(f"    worker type    = {pi['worker_type']}")
    print(f"    driver count   = {pi['driver_count']}")
    print(f"    executor count = {pi['executor_count']}")
    print(f"    note: {pi['note']}")
    print()
    ds = result["dominant_worker_shape"]
    print(f"  Dominant worker shape: {ds['shape']} ({ds['jobs_using_it']}/{ds['of']} jobs)")
    if result["consistency_warnings"]:
        print()
        print(f"  ⚠ Consistency warnings:")
        for w in result["consistency_warnings"]:
            print(f"    - {w}")
    print()
    print(f"  Top jobs by peak demand:")
    print(f"    {'Job':<40} {'vCPU':>6} {'Mem(GB)':>8} {'Disk(GB)':>9} {'Workers':>8}")
    for j in result["per_job_peak_demand"][:10]:
        name = j["job"][:38]
        print(f"    {name:<40} {j['cpu_vcpu']:>6} {j['memory_gb']:>8.0f} {j['disk_gb']:>9.0f} {j['max_executors']:>8}")
    print(f"{'='*72}\n")


def main():
    p = argparse.ArgumentParser(description="Consolidate per-job Fine Tuner recs into EMR Serverless application settings")
    p.add_argument("--input", required=True, help="Recommendations JSON file (list) or directory of JSONs")
    p.add_argument("--concurrency", default="sequential",
                   help="'sequential' (default), 'peak-concurrent', or an integer N (max concurrent jobs)")
    p.add_argument("--headroom", type=float, default=20.0, help="Headroom percent on the capacity ceiling (default 20)")
    p.add_argument("--pre-init", action="store_true", help="Emphasize the pre-initialized capacity recommendation")
    p.add_argument("--output", help="Write the consolidated settings JSON to this path")
    args = p.parse_args()

    # Validate concurrency
    if args.concurrency not in ("sequential", "peak-concurrent"):
        if not args.concurrency.isdigit() or int(args.concurrency) < 1:
            p.error("--concurrency must be 'sequential', 'peak-concurrent', or a positive integer")

    recs = load_recommendations(args.input)
    jobs = [d for d in (job_demand_from_rec(r) for r in recs) if d is not None]
    if not jobs:
        p.error("No valid job recommendations found in input (need a `worker` block or configuration.spark_conf)")

    result = consolidate(jobs, args.concurrency, args.headroom, args.concurrency)
    print_report(result)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Wrote consolidated settings to {args.output}")


if __name__ == "__main__":
    main()
