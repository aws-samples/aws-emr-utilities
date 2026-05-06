#!/usr/bin/env python3
"""
EMR Serverless Config Advisor — End-to-End Pipeline
====================================================
Three-stage pipeline:
1. Extract metrics from Spark event logs → Intermediate JSON (python_extractor.py)
2. Generate EMR Serverless recommendations (emr_recommender.py)
3. Format to job config (format_to_job_config.py)

Usage:
    python3 pipeline_wrapper.py --input s3://bucket/event-logs/ --output s3://bucket/extracted/
    python3 pipeline_wrapper.py --input s3://bucket/event-logs/ --output s3://bucket/extracted/ --format-job-config
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent


def run_command(cmd, description):
    """Execute a command and handle errors."""
    print(f"\n{'='*80}")
    print(f"STEP: {description}")
    print(f"{'='*80}")
    print(f"Command: {' '.join(cmd)}\n")

    start_time = time.time()
    try:
        subprocess.run(cmd, check=True, text=True)
        elapsed = time.time() - start_time
        print(f"\n✓ {description} completed in {elapsed:.1f}s")
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n✗ {description} failed with exit code {e.returncode}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="EMR Serverless Config Advisor — End-to-End Pipeline"
    )
    parser.add_argument("--input", required=True, help="S3 or local path to event logs")
    parser.add_argument("--output", required=True, help="Output path for extracted metrics (S3 or local)")
    parser.add_argument("--limit", type=int, default=100, help="Max apps to process (default: 100)")
    parser.add_argument("--workers", type=int, default=20, help="Parallel workers for extraction (default: 20)")
    parser.add_argument("--profile", default=None, help="AWS profile name")
    parser.add_argument("--single-app", action="store_true", help="Treat --input as a single app path")
    parser.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")
    parser.add_argument("--target-partition-size", type=int, default=1024, help="Target shuffle partition size in MiB (default: 1024)")
    parser.add_argument("--results", default="recommendations.json", help="Output filename for recommendations (default: recommendations.json)")
    parser.add_argument("--format-job-config", action="store_true", help="Also format output to job config JSON")
    parser.add_argument("--cost-optimized", action="store_true", help="Generate only cost-optimized recommendations")
    parser.add_argument("--performance-optimized", action="store_true", help="Generate only performance-optimized recommendations")
    parser.add_argument("--individual-files", action="store_true", help="Generate individual JSON files per job")
    parser.add_argument("--write-to-iceberg-table", help="Write recommendations to Iceberg table (catalog.database.table)")
    parser.add_argument("--serverless-storage", action="store_true", help="Enable serverless storage recommendations (disabled by default)")
    parser.add_argument("--skip-extraction", action="store_true", help="Skip extraction step (use existing data)")

    args = parser.parse_args()

    cost_file = args.results.replace(".json", "_cost.json")
    perf_file = args.results.replace(".json", "_perf.json")

    print(f"\n{'='*80}")
    print("EMR SERVERLESS CONFIG ADVISOR — PIPELINE")
    print(f"{'='*80}")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Input:   {args.input}")
    print(f"  Output:  {args.output}")
    print(f"  Results: {cost_file}, {perf_file}")
    print(f"  Limit:   {args.limit} applications")

    pipeline_start = time.time()

    # Stage 1: Extract
    if not args.skip_extraction:
        cmd = [
            sys.executable, str(SCRIPT_DIR / "python_extractor.py"),
            "--input", args.input,
            "--output", args.output,
            "--limit", str(args.limit),
            "--workers", str(args.workers),
        ]
        if args.single_app:
            cmd.append("--single-app")
        if args.profile:
            cmd.extend(["--profile", args.profile])

        if not run_command(cmd, "Stage 1: Extract metrics from event logs"):
            sys.exit(1)
    else:
        print("\n⊘ Skipping extraction (using existing data)")

    # Stage 2: Recommend
    cmd = [
        sys.executable, str(SCRIPT_DIR / "emr_recommender.py"),
        "--input-path", args.output,
        "--output-cost", cost_file,
        "--output-perf", perf_file,
        "--limit", str(args.limit),
        "--region", args.region,
        "--target-partition-size", str(args.target_partition_size),
    ]
    if args.cost_optimized:
        cmd.append("--cost-optimized")
    if args.performance_optimized:
        cmd.append("--performance-optimized")
    if args.individual_files:
        cmd.append("--individual-files")
    if args.write_to_iceberg_table:
        cmd.extend(["--write-to-iceberg-table", args.write_to_iceberg_table])
    if args.serverless_storage:
        cmd.append("--serverless-storage")

    if not run_command(cmd, "Stage 2: Generate recommendations"):
        sys.exit(1)

    # Stage 3: Format to job config (optional)
    if args.format_job_config:
        for label, src in [("cost", cost_file), ("perf", perf_file)]:
            dst = src.replace(".json", "_job_config.json")
            cmd = [
                sys.executable, str(SCRIPT_DIR / "format_to_job_config.py"),
                "--input", src,
                "--output", dst,
            ]
            if not run_command(cmd, f"Stage 3: Format {label} → job config"):
                sys.exit(1)

    elapsed = time.time() - pipeline_start
    print(f"\n{'='*80}")
    print("PIPELINE COMPLETE")
    print(f"{'='*80}")
    print(f"Total time: {elapsed:.1f}s ({elapsed/60:.1f} minutes)")
    print(f"\nOutputs:")
    print(f"  Metrics:      {args.output}")
    print(f"  Cost:         {cost_file}")
    print(f"  Performance:  {perf_file}")
    if args.format_job_config:
        print(f"  Job configs:  {cost_file.replace('.json', '_job_config.json')}, {perf_file.replace('.json', '_job_config.json')}")
    print(f"\n✅ Pipeline completed successfully!\n")


if __name__ == "__main__":
    main()
