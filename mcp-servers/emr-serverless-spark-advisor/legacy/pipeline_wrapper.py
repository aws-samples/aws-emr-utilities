#!/usr/bin/env python3
"""
Spark Event Log Processing & Recommendation Pipeline
====================================================
Two-stage pipeline:
1. Extract metrics from Spark event logs (S3) → Intermediate JSON
2. Generate EMR Serverless recommendations from extracted metrics

Usage:
    python3 pipeline_wrapper.py --input-bucket suthan-event-logs --input-prefix main/apps/
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Pipeline configuration
SPARK_PROCESSOR_SCRIPT = "spark_processor.py"
SPARK_EXTRACTOR_SCRIPT = "spark_extractor.py"
RECOMMENDER_SCRIPT = "emr_recommender.py"

# Default S3 locations
DEFAULT_INPUT_BUCKET = "suthan-event-logs"
DEFAULT_INPUT_PREFIX = "main/apps/"
DEFAULT_STAGING_BUCKET = "suthan-event-logs"
DEFAULT_STAGING_PREFIX = "pipeline-staging/"
DEFAULT_OUTPUT_FILE = "emr_recommendations.json"


def run_command(cmd, description):
    """Execute a command and handle errors."""
    import os
    print(f"\n{'='*80}")
    print(f"STEP: {description}")
    print(f"{'='*80}")
    print(f"Command: {' '.join(cmd)}\n")
    
    start_time = time.time()
    
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=False,
            text=True,
            env=os.environ.copy()  # Inherit environment variables
        )
        elapsed = time.time() - start_time
        print(f"\n✓ {description} completed in {elapsed:.1f}s")
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n✗ {description} failed with exit code {e.returncode}")
        print(f"Error: {e}")
        return False


def update_spark_processor_config(input_path, output_path):
    """Update spark_processor.py configuration dynamically."""
    print(f"\n{'='*80}")
    print("CONFIGURATION: Updating spark_processor.py")
    print(f"{'='*80}")
    
    script_path = Path(SPARK_PROCESSOR_SCRIPT)
    if not script_path.exists():
        print(f"✗ Error: {SPARK_PROCESSOR_SCRIPT} not found")
        return False
    
    with open(script_path, 'r') as f:
        content = f.read()
    
    # Update configuration
    import re
    content = re.sub(
        r'INPUT_PATH = "[^"]*"',
        f'INPUT_PATH = "{input_path}"',
        content
    )
    content = re.sub(
        r'OUTPUT_PATH = "[^"]*"',
        f'OUTPUT_PATH = "{output_path}"',
        content
    )
    
    # Write updated config
    with open(script_path, 'w') as f:
        f.write(content)
    
    print(f"✓ Updated configuration:")
    print(f"  INPUT:   {input_path}")
    print(f"  OUTPUT:  {output_path}")
    
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Spark Event Log Processing & EMR Recommendation Pipeline"
    )
    
    # Unified path arguments (support both local and S3)
    parser.add_argument(
        "--input-path",
        help="Input path: s3://bucket/prefix or /local/path (overrides --input-bucket/prefix)"
    )
    parser.add_argument(
        "--output-path",
        help="Output path: s3://bucket/prefix or /local/path (overrides --staging-bucket/prefix)"
    )
    
    # Legacy S3-specific arguments (for backward compatibility)
    parser.add_argument(
        "--input-bucket",
        default=DEFAULT_INPUT_BUCKET,
        help=f"S3 bucket with event logs (default: {DEFAULT_INPUT_BUCKET})"
    )
    parser.add_argument(
        "--input-prefix",
        default=DEFAULT_INPUT_PREFIX,
        help=f"S3 prefix for event logs (default: {DEFAULT_INPUT_PREFIX})"
    )
    
    # Staging configuration
    parser.add_argument(
        "--staging-bucket",
        default=DEFAULT_STAGING_BUCKET,
        help=f"S3 bucket for intermediate JSON (default: {DEFAULT_STAGING_BUCKET})"
    )
    parser.add_argument(
        "--staging-prefix",
        default=DEFAULT_STAGING_PREFIX,
        help=f"S3 prefix for staging (default: {DEFAULT_STAGING_PREFIX})"
    )
    
    # Output configuration
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_FILE,
        help=f"Output file for recommendations (default: {DEFAULT_OUTPUT_FILE})"
    )
    
    # Processing options
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Number of largest jobs to analyze (default: 100)"
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region (default: us-east-1)"
    )
    parser.add_argument(
        "--target-partition-size",
        type=int,
        default=1024,
        help="Target shuffle partition size in MiB (default: 1024)"
    )
    parser.add_argument(
        "--last-hours",
        type=int,
        help="Process only event logs modified in last N hours (1, 2, 24, 168 for 1 week, etc.)"
    )
    parser.add_argument(
        "--format-job-config",
        action="store_true",
        help="Format output to job configuration format"
    )
    parser.add_argument(
        "--cost-optimized",
        action="store_true",
        help="Generate only cost-optimized recommendations"
    )
    parser.add_argument(
        "--performance-optimized",
        action="store_true",
        help="Generate only performance-optimized recommendations"
    )
    parser.add_argument(
        "--individual-files",
        action="store_true",
        help="Generate individual JSON files per job (1-jobname.json, 2-jobname.json, ...)"
    )
    parser.add_argument(
        "--write-to-iceberg-table",
        help="Write recommendations to Iceberg table (catalog.database.table)"
    )
    parser.add_argument(
        "--skip-extraction",
        action="store_true",
        help="Skip extraction step (use existing staging data)"
    )
    
    args = parser.parse_args()
    
    # Resolve paths (unified or legacy)
    if args.input_path:
        input_path = args.input_path
    else:
        input_path = f"s3://{args.input_bucket}/{args.input_prefix}"
    
    if args.output_path:
        output_path = args.output_path
    else:
        output_path = f"s3://{args.staging_bucket}/{args.staging_prefix}"
    
    print(f"\n{'='*80}")
    print("SPARK EVENT LOG PROCESSING & RECOMMENDATION PIPELINE")
    print(f"{'='*80}")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"\nConfiguration:")
    print(f"  Input:   {input_path}")
    print(f"  Output:  {output_path}")
    print(f"  Results: {args.output}")
    print(f"  Limit:   {args.limit} applications")
    
    pipeline_start = time.time()
    
    # Stage 1: Extract metrics from event logs
    if not args.skip_extraction:
        print(f"\n{'='*80}")
        print("STAGE 1: EXTRACT METRICS FROM EVENT LOGS")
        print(f"{'='*80}")
        
        # Check if spark-submit is available for fast extraction
        import shutil
        use_spark = shutil.which("spark-submit") and Path(SPARK_EXTRACTOR_SCRIPT).exists()
        
        if use_spark:
            print("Using PySpark extractor (parallel decompression + aggregation)")
            cmd = [
                "spark-submit",
                "--master", "local[*]",
                "--conf", "spark.driver.memory=32g",
                "--conf", "spark.sql.adaptive.enabled=true",
                SPARK_EXTRACTOR_SCRIPT,
                "--input", input_path,
                "--output", output_path,
                "--limit", str(args.limit),
            ]
            if not run_command(cmd, "Spark Metric Extraction"):
                print("\n⚠ Spark extraction failed, falling back to Python extractor...")
                use_spark = False
        
        if not use_spark:
            print("Using Python extractor (sequential)")
            # Update spark processor configuration
            if not update_spark_processor_config(input_path, output_path):
                print("\n✗ Pipeline failed: Configuration update failed")
                sys.exit(1)
            
            cmd = ["python3", SPARK_PROCESSOR_SCRIPT]
            if args.last_hours:
                cmd.extend(["--last-hours", str(args.last_hours)])
            if not run_command(cmd, "Metric Extraction"):
                print("\n✗ Pipeline failed: Metric extraction failed")
                sys.exit(1)
    else:
        print(f"\n⊘ Skipping extraction (using existing data in staging)")
    
    # Stage 2: Generate recommendations
    print(f"\n{'='*80}")
    print("STAGE 2: GENERATE EMR SERVERLESS RECOMMENDATIONS")
    print(f"{'='*80}")
    
    cmd = [
        "python3",
        RECOMMENDER_SCRIPT,
        "--input-path", output_path,
        "--output-cost", args.output.replace('.json', '_cost.json'),
        "--output-perf", args.output.replace('.json', '_perf.json'),
        "--limit", str(args.limit),
        "--target-partition-size", str(args.target_partition_size)
    ]
    
    if args.region:
        cmd.extend(["--region", args.region])
    
    if args.format_job_config:
        cmd.append("--format-job-config")
    
    if args.cost_optimized:
        cmd.append("--cost-optimized")
    
    if args.performance_optimized:
        cmd.append("--performance-optimized")
    
    if args.individual_files:
        cmd.append("--individual-files")
    
    if args.write_to_iceberg_table:
        cmd.extend(["--write-to-iceberg-table", args.write_to_iceberg_table])
    
    if not run_command(cmd, "Recommendation Generation"):
        print("\n✗ Pipeline failed: Recommendation generation failed")
        sys.exit(1)
    
    # Pipeline complete
    elapsed = time.time() - pipeline_start
    print(f"\n{'='*80}")
    print("PIPELINE COMPLETE")
    print(f"{'='*80}")
    print(f"Total time: {elapsed:.1f}s ({elapsed/60:.1f} minutes)")
    print(f"\nOutputs:")
    print(f"  Metrics: {output_path}")
    print(f"  Cost-optimized: {args.output.replace('.json', '_cost.json')}")
    print(f"  Performance-optimized: {args.output.replace('.json', '_perf.json')}")
    print(f"\n✅ Pipeline completed successfully!\n")


if __name__ == "__main__":
    main()
