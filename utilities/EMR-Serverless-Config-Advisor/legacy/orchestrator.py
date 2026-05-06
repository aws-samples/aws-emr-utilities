#!/usr/bin/env python3
"""
Parallel orchestrator — discovers apps from S3, launches one spark-submit
per app in YARN cluster mode, polls for completion.

Usage (EMR on EC2):
    python3 orchestrator.py \
        --input s3://bucket/event-logs/ \
        --output s3://bucket/output/ \
        --script s3://bucket/scripts/spark_extractor.py \
        --parallelism 12

Usage (EMR Serverless — future):
    python3 orchestrator.py \
        --input s3://bucket/event-logs/ \
        --output s3://bucket/output/ \
        --mode serverless \
        --application-id 00gXXX \
        --execution-role arn:aws:iam::XXX:role/YYY
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3


def discover_apps(input_path, limit):
    """Discover app prefixes from S3."""
    parts = input_path.replace("s3://", "").split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    s3 = boto3.client("s3", region_name="us-east-1")
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
    apps = []

    for cp in resp.get("CommonPrefixes", []):
        p = cp["Prefix"]
        name = p.rstrip("/").rsplit("/", 1)[-1]
        if name.startswith(("eventlog_v2_", "application_")):
            apps.append((f"s3://{bucket}/{p}", name))

    for obj in resp.get("Contents", []):
        k = obj["Key"]
        name = k.rsplit("/", 1)[-1]
        if name.startswith("application_") and not k.endswith("/"):
            apps.append((f"s3://{bucket}/{k}", name))

    return apps[:limit]


def submit_yarn_job(app_s3_path, app_name, script_path, output_path,
                    driver_memory="4g", executor_memory="4g", num_executors=2):
    """Submit a single-app spark-submit in YARN cluster mode. Returns (app_name, process)."""
    cmd = [
        "spark-submit",
        "--master", "yarn",
        "--deploy-mode", "cluster",
        "--driver-memory", driver_memory,
        "--executor-memory", executor_memory,
        "--num-executors", str(num_executors),
        "--driver-java-options", "-Xss8m",
        "--conf", "spark.executor.extraJavaOptions=-Xss8m",
        "--archives", "s3://suthan-event-logs/scripts/zstandard.zip#deps",
        "--conf", "spark.yarn.submit.waitAppCompletion=true",
        "--conf", "spark.sql.adaptive.enabled=true",
        "--conf", "spark.yarn.appMasterEnv.PYTHONPATH=./deps",
        "--conf", "spark.executorEnv.PYTHONPATH=./deps",
        script_path,
        "--input", app_s3_path,
        "--output", output_path,
        "--single-app",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return app_name, proc


def run_yarn_parallel(apps, script_path, output_path, parallelism,
                      driver_memory, executor_memory, num_executors):
    """Launch apps in parallel on YARN, up to `parallelism` at a time."""
    print(f"\nLaunching {len(apps)} apps on YARN (parallelism={parallelism})")
    print(f"  driver={driver_memory}, executor={executor_memory}, num-executors={num_executors}")

    results = {}
    start = time.time()

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {}
        for app_s3_path, app_name in apps:
            f = pool.submit(submit_yarn_job, app_s3_path, app_name, script_path,
                            output_path, driver_memory, executor_memory, num_executors)
            futures[f] = app_name

        for f in as_completed(futures):
            app_name, proc = f.result()
            rc = proc.wait()
            elapsed = time.time() - start
            if rc == 0:
                print(f"  ✓ {app_name} (exit=0, {elapsed:.0f}s elapsed)")
                results[app_name] = "SUCCESS"
            else:
                stderr = proc.stderr.read().decode()[-500:]
                print(f"  ✗ {app_name} (exit={rc}, {elapsed:.0f}s elapsed)")
                print(f"    {stderr[-200:]}")
                results[app_name] = f"FAILED (rc={rc})"

    elapsed = time.time() - start
    success = sum(1 for v in results.values() if v == "SUCCESS")
    print(f"\n{'='*60}")
    print(f"Done: {success}/{len(apps)} succeeded in {elapsed:.1f}s ({elapsed/60:.1f} min)")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parallel spark-submit orchestrator")
    parser.add_argument("--input", required=True, help="S3 path containing event logs")
    parser.add_argument("--output", required=True, help="S3 output path for results")
    parser.add_argument("--script", required=True, help="Path to spark_extractor.py (local or S3)")
    parser.add_argument("--limit", type=int, default=100, help="Max apps to process")
    parser.add_argument("--parallelism", type=int, default=6, help="Max concurrent spark-submit jobs")
    parser.add_argument("--driver-memory", default="4g")
    parser.add_argument("--executor-memory", default="4g")
    parser.add_argument("--num-executors", type=int, default=2)
    args = parser.parse_args()

    print(f"{'='*60}")
    print("PARALLEL SPARK EXTRACTOR ORCHESTRATOR")
    print(f"{'='*60}")

    apps = discover_apps(args.input, args.limit)
    print(f"Discovered {len(apps)} apps:")
    for path, name in apps:
        print(f"  {name}")

    results = run_yarn_parallel(
        apps, args.script, args.output, args.parallelism,
        args.driver_memory, args.executor_memory, args.num_executors,
    )
