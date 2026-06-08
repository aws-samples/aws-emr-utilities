#!/usr/bin/env python3
"""
Submit TPC-DS queries as individual EMR Serverless jobs with per-query recommended configs.

Usage:
    python3 submit_benchmark.py \
        --recommendations /tmp/recs_v5_cost.json \
        --application-id YOUR-APP-ID \
        --output-prefix s3://YOUR-BUCKET/benchmarking/output/ \
        --run-name v5-partitions-1000
"""

import argparse
import json
import subprocess
import time
import sys

# TPC-DS query list (104 queries)
TPCDS_QUERIES = [
    f"q{i}-v2.4" for i in range(1, 100)
] + ["q14a-v2.4", "q14b-v2.4", "q23a-v2.4", "q23b-v2.4", "q24a-v2.4", "q24b-v2.4", "q39a-v2.4", "q39b-v2.4"]

DATA_PATH = "s3://YOUR-BUCKET/data/BLOG_TPCDS-TEST-3T-partitioned"
JAR_PATH = "s3://YOUR-BUCKET/jars/spark-benchmark-assembly-3.3.0.jar"
ROLE_ARN = "arn:aws:iam::ACCOUNT_ID:role/EMRServerlessS3RuntimeRole"
REGION = "us-east-1"


def build_spark_params(cfg):
    """Convert spark_configs dict to sparkSubmitParameters string."""
    params = f"--class com.amazonaws.eks.tpcds.BenchmarkSQL"
    # Core configs from recommendation
    keys = [
        "spark.driver.cores", "spark.driver.memory",
        "spark.executor.cores", "spark.executor.memory",
        "spark.dynamicAllocation.enabled", "spark.dynamicAllocation.maxExecutors",
        "spark.dynamicAllocation.minExecutors", "spark.dynamicAllocation.initialExecutors",
        "spark.sql.adaptive.enabled", "spark.sql.adaptive.coalescePartitions.parallelismFirst",
        "spark.sql.shuffle.partitions", "spark.sql.files.maxPartitionBytes",
        "spark.emr-serverless.executor.disk", "spark.emr-serverless.executor.disk.type",
        "spark.network.timeout", "spark.shuffle.io.connectionTimeout",
    ]
    for k in keys:
        v = cfg.get(k)
        if v:
            params += f" --conf {k}={v}"
    return params


def submit_job(app_id, query, spark_params, output_prefix, run_name):
    """Submit a single EMR Serverless job."""
    cmd = [
        "aws", "emr-serverless", "start-job-run",
        "--application-id", app_id,
        "--execution-role-arn", ROLE_ARN,
        "--name", f"{run_name}-{query}",
        "--region", REGION,
        "--job-driver", json.dumps({
            "sparkSubmit": {
                "entryPoint": JAR_PATH,
                "entryPointArguments": [
                    DATA_PATH,
                    f"{output_prefix}{query.replace('-v2.4', '')}/",
                    "/opt/tpcds-kit/tools",
                    "parquet", "3000", "1", "false",
                    query,
                    "true"
                ],
                "sparkSubmitParameters": spark_params
            }
        })
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        resp = json.loads(result.stdout)
        return resp["jobRunId"]
    else:
        print(f"  ERROR: {result.stderr[:200]}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recommendations", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--queries", help="Comma-separated query list (default: all 104)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.recommendations) as f:
        recs = json.load(f)

    # All recs have same partition/memory since we used one-size config
    # Use first valid recommendation as the default config
    default_cfg = None
    for r in recs:
        cfg = r.get("spark_configs", {})
        if cfg.get("spark.sql.shuffle.partitions"):
            default_cfg = cfg
            break

    if not default_cfg:
        print("ERROR: No valid recommendation found")
        sys.exit(1)

    queries = args.queries.split(",") if args.queries else TPCDS_QUERIES
    spark_params = build_spark_params(default_cfg)

    print(f"Submitting {len(queries)} queries to {args.application_id}")
    print(f"Output: {args.output_prefix}")
    print(f"Config: maxExec={default_cfg.get('spark.dynamicAllocation.maxExecutors')}, "
          f"mem={default_cfg.get('spark.executor.memory')}, "
          f"partitions={default_cfg.get('spark.sql.shuffle.partitions')}")
    print()

    if args.dry_run:
        print(f"DRY RUN — sparkSubmitParameters:")
        print(f"  {spark_params}")
        return

    submitted = {}
    for i, q in enumerate(queries, 1):
        job_id = submit_job(args.application_id, q, spark_params, args.output_prefix, args.run_name)
        if job_id:
            submitted[q] = job_id
            print(f"  [{i}/{len(queries)}] {q}: {job_id}")
        else:
            print(f"  [{i}/{len(queries)}] {q}: FAILED TO SUBMIT")
        # Small delay to avoid throttling
        if i % 10 == 0:
            time.sleep(2)

    # Save submission manifest
    manifest = {
        "run_name": args.run_name,
        "application_id": args.application_id,
        "output_prefix": args.output_prefix,
        "spark_configs": default_cfg,
        "submitted_jobs": submitted,
        "total_queries": len(queries),
        "submitted_count": len(submitted),
    }
    manifest_path = f"/tmp/{args.run_name}_manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest: {manifest_path}")
    print(f"Submitted: {len(submitted)}/{len(queries)}")


if __name__ == "__main__":
    main()
