"""Lambda orchestrator for EMR Serverless parallel extraction.
Discovers apps from S3, launches one EMR Serverless job per app, polls for completion.
"""
import json
import time
import boto3

def lambda_handler(event, context):
    input_path = event["input_path"]          # s3://bucket/prefix/
    output_path = event["output_path"]        # s3://bucket/output/
    app_id = event["application_id"]          # EMR Serverless app ID
    execution_role = event["execution_role"]  # IAM role ARN
    script_path = event.get("script_path", "s3://suthan-event-logs/scripts/spark_extractor.py")
    zstd_archive = event.get("zstd_archive", "s3://suthan-event-logs/scripts/zstandard.zip")

    # Discover apps
    parts = input_path.replace("s3://", "").split("/", 1)
    bucket, prefix = parts[0], parts[1].rstrip("/") + "/"
    s3 = boto3.client("s3", region_name="us-east-1")
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")

    apps = []
    for cp in resp.get("CommonPrefixes", []):
        name = cp["Prefix"].rstrip("/").rsplit("/", 1)[-1]
        if name.startswith(("eventlog_v2_", "application_")):
            apps.append((f"s3://{bucket}/{cp['Prefix']}", name))
    for obj in resp.get("Contents", []):
        name = obj["Key"].rsplit("/", 1)[-1]
        if name.startswith("application_") and not obj["Key"].endswith("/"):
            apps.append((f"s3://{bucket}/{obj['Key']}", name))

    print(f"Discovered {len(apps)} apps")

    # Launch all jobs
    emr = boto3.client("emr-serverless", region_name="us-east-1")
    spark_params = (
        f"--conf spark.archives={zstd_archive}#deps "
        f"--conf spark.emr-serverless.driverEnv.PYTHONPATH=./deps "
        f"--conf spark.executorEnv.PYTHONPATH=./deps "
        f"--conf spark.driver.extraJavaOptions=-Xss8m "
        f"--conf spark.executor.extraJavaOptions=-Xss8m"
    )

    jobs = {}
    for s3_path, name in apps:
        resp = emr.start_job_run(
            applicationId=app_id,
            executionRoleArn=execution_role,
            jobDriver={
                "sparkSubmit": {
                    "entryPoint": script_path,
                    "entryPointArguments": ["--input", s3_path, "--output", output_path, "--single-app"],
                    "sparkSubmitParameters": spark_params,
                }
            },
            name=f"extract-{name[:40]}",
        )
        jobs[resp["jobRunId"]] = name
        print(f"  Launched {name}: {resp['jobRunId']}")

    # Poll for completion
    start = time.time()
    pending = set(jobs.keys())
    results = {}
    while pending and (time.time() - start) < 840:  # 14 min max (Lambda 15 min limit)
        time.sleep(15)
        for jid in list(pending):
            state = emr.get_job_run(applicationId=app_id, jobRunId=jid)["jobRun"]["state"]
            if state in ("SUCCESS", "FAILED", "CANCELLED"):
                elapsed = time.time() - start
                results[jid] = state
                pending.remove(jid)
                print(f"  {jobs[jid]}: {state} ({elapsed:.0f}s)")

    elapsed = time.time() - start
    succeeded = sum(1 for s in results.values() if s == "SUCCESS")
    failed = sum(1 for s in results.values() if s == "FAILED")
    still_running = len(pending)

    summary = {
        "total": len(apps),
        "succeeded": succeeded,
        "failed": failed,
        "still_running": still_running,
        "elapsed_seconds": round(elapsed, 1),
        "jobs": {jid: {"name": jobs[jid], "state": results.get(jid, "RUNNING")} for jid in jobs},
    }
    print(f"\nDone: {succeeded}/{len(apps)} succeeded, {failed} failed, {still_running} running in {elapsed:.0f}s")
    return summary
