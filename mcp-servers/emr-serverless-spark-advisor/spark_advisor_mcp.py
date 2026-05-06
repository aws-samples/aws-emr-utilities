#!/usr/bin/env python3
"""
EMR Serverless Spark Config Advisor — MCP Server

Analyzes Spark event logs from S3 via EMR Serverless, generates optimized
EMR Serverless configurations, and provides interactive querying, comparison,
and bottleneck analysis through 12 MCP tools.

Configuration via environment variables:
    EMR_SERVERLESS_APP_ID:  EMR Serverless application ID
    EMR_EXECUTION_ROLE:     IAM role ARN for EMR Serverless jobs
    SCRIPT_S3_PATH:         S3 path to spark_extractor.py
    ARCHIVES_S3_PATH:       S3 path to zstandard.zip (optional)
    OUTPUT_S3_PATH:         S3 base path for output (e.g. s3://bucket/advisor/)
    AWS_REGION:             AWS region (default: us-east-1)
"""

import json, os, time, logging
import boto3
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

log = logging.getLogger("spark-advisor-mcp")

APP_ID = os.environ.get("EMR_SERVERLESS_APP_ID", "")
EXEC_ROLE = os.environ.get("EMR_EXECUTION_ROLE", "")
SCRIPT_PATH = os.environ.get("SCRIPT_S3_PATH", "")
ARCHIVES_PATH = os.environ.get("ARCHIVES_S3_PATH", "")
OUTPUT_BASE = os.environ.get("OUTPUT_S3_PATH", "").rstrip("/")
REGION = os.environ.get("ADVISOR_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))

emr = boto3.client("emr-serverless", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)

mcp = FastMCP(
    "spark-config-advisor",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# ── S3 helpers ────────────────────────────────────────────────────────

def _s3_parts(path):
    p = path.replace("s3://", "").split("/", 1)
    return p[0], p[1] if len(p) > 1 else ""

def _s3_read_json(path):
    bucket, key = _s3_parts(path)
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body)
    except Exception:
        return None

def _s3_list_jsons(path):
    bucket, prefix = _s3_parts(path.rstrip("/") + "/")
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return [o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".json")]

def _output_path():
    return OUTPUT_BASE

def _load_app(app_id, subdir="task_stage_summary"):
    base = _output_path()
    for name in [app_id, f"eventlog_v2_{app_id}"]:
        data = _s3_read_json(f"{base}/{subdir}/{name}.json")
        if data:
            return data
    return None

def _list_apps():
    base = _output_path()
    keys = _s3_list_jsons(f"{base}/task_stage_summary/")
    return [k.split("/")[-1].replace(".json", "") for k in keys]

# ── EMR Serverless job submission ─────────────────────────────────────

def _submit_job(input_path, output_path):
    """Submit a single EMR Serverless spark_extractor job and wait."""
    args = ["--input", input_path, "--output", output_path]
    job_params = {
        "sparkSubmit": {
            "entryPoint": SCRIPT_PATH,
            "entryPointArguments": args,
            "sparkSubmitParameters": (
                "--conf spark.executor.cores=8 "
                "--conf spark.executor.memory=54g "
                "--conf spark.driver.cores=8 "
                "--conf spark.driver.memory=16g "
                "--conf spark.dynamicAllocation.maxExecutors=100 "
                "--conf spark.emr-serverless.executor.disk=200g"
            ),
        }
    }
    if ARCHIVES_PATH:
        job_params["sparkSubmit"]["sparkSubmitParameters"] += (
            f" --archives {ARCHIVES_PATH}#zstandard_lib"
            " --conf spark.emr-serverless.driverEnv.PYTHONPATH=./zstandard_lib"
            " --conf spark.executorEnv.PYTHONPATH=./zstandard_lib"
        )

    resp = emr.start_job_run(
        applicationId=APP_ID,
        executionRoleArn=EXEC_ROLE,
        jobDriver=job_params,
        configurationOverrides={"monitoringConfiguration": {"managedPersistenceMonitoringConfiguration": {"enabled": True}}},
    )
    job_id = resp["jobRunId"]
    log.info("Submitted job %s for %s", job_id, input_path)

    while True:
        status = emr.get_job_run(applicationId=APP_ID, jobRunId=job_id)
        state = status["jobRun"]["state"]
        if state in ("SUCCESS", "FAILED", "CANCELLED"):
            break
        time.sleep(10)

    return job_id, state

def _submit_parallel_jobs(app_prefixes, output_path):
    """Submit one job per app prefix in parallel, wait for all."""
    import concurrent.futures
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = {}
        for prefix in app_prefixes:
            app_input = f"s3://{prefix}"
            f = pool.submit(_submit_job, app_input, output_path)
            futures[f] = prefix
        for f in concurrent.futures.as_completed(futures):
            prefix = futures[f]
            try:
                job_id, state = f.result()
                results[prefix] = {"job_id": job_id, "state": state}
            except Exception as e:
                results[prefix] = {"job_id": None, "state": f"ERROR: {e}"}
    return results

def _discover_apps(input_path):
    """List app prefixes under an S3 path. Handles directories, single apps, and zip files."""
    bucket, prefix = _s3_parts(input_path.rstrip("/"))

    # Handle zip files: download, extract, upload contents, return discovered apps
    if prefix.endswith(".zip"):
        return _extract_zip_to_s3(bucket, prefix)

    prefix = prefix.rstrip("/") + "/"

    # Check if input_path itself is a single app directory
    dir_name = prefix.rstrip("/").split("/")[-1]
    if dir_name.startswith("eventlog_v2_") or dir_name.startswith("application_"):
        return [f"{bucket}/{prefix}"]

    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
    apps = []
    for cp in resp.get("CommonPrefixes", []):
        p = cp["Prefix"]
        name = p.rstrip("/").split("/")[-1]
        if name.startswith("eventlog_v2_") or name.startswith("application_"):
            apps.append(f"{bucket}/{p}")
    return apps


def _extract_zip_to_s3(bucket, zip_key):
    """Download a zip from S3, extract it, upload event log dirs back to S3."""
    import zipfile, tempfile, io

    log.info("Downloading zip s3://%s/%s", bucket, zip_key)
    zip_obj = s3.get_object(Bucket=bucket, Key=zip_key)["Body"].read()

    # Determine upload prefix: same directory as the zip, under extracted/
    zip_dir = "/".join(zip_key.split("/")[:-1])
    zip_name = zip_key.split("/")[-1].replace(".zip", "")
    upload_prefix = f"{zip_dir}/{zip_name}/" if zip_dir else f"{zip_name}/"

    apps = set()
    with zipfile.ZipFile(io.BytesIO(zip_obj)) as zf:
        for name in zf.namelist():
            # Find event log app directories
            parts = name.split("/")
            app_dir = None
            for p in parts:
                if p.startswith("eventlog_v2_") or p.startswith("application_"):
                    app_dir = p
                    break
            if not app_dir:
                continue

            # Upload file to S3 under the extracted prefix
            if not name.endswith("/"):
                # Rebuild path relative to the app dir
                idx = parts.index(app_dir)
                rel_path = "/".join(parts[idx:])
                s3_key = f"{upload_prefix}{rel_path}"
                data = zf.read(name)
                s3.put_object(Bucket=bucket, Key=s3_key, Body=data)
                apps.add(f"{bucket}/{upload_prefix}{app_dir}/")

    log.info("Extracted %d app(s) from zip to s3://%s/%s", len(apps), bucket, upload_prefix)
    return list(apps)


# ── Tool: Extraction & Recommendations ───────────────────────────────

@mcp.tool()
def analyze_spark_logs(
    input_path: str,
    mode: str = "cost-optimized",
    limit: int = 100,
) -> str:
    """Analyze Spark event logs from S3 and generate EMR Serverless configuration recommendations.

    Submits parallel EMR Serverless jobs to extract metrics, then generates recommendations.
    Results are cached in S3 for interactive querying with other tools.

    Args:
        input_path: S3 path to event logs (e.g. s3://bucket/event-logs/)
        mode: 'cost-optimized' or 'performance-optimized'
        limit: Max applications to analyze (default 100)

    Returns:
        JSON with recommendations per application.
    """
    if not input_path.startswith("s3://"):
        return json.dumps({"error": "input_path must be an S3 path (s3://bucket/prefix/)"})
    if not APP_ID or not EXEC_ROLE or not SCRIPT_PATH:
        return json.dumps({"error": "Missing config: EMR_SERVERLESS_APP_ID, EMR_EXECUTION_ROLE, SCRIPT_S3_PATH"})

    output = _output_path()

    # Discover apps
    app_prefixes = _discover_apps(input_path)[:limit]
    if not app_prefixes:
        return json.dumps({"error": f"No event log apps found at {input_path}"})

    # Submit parallel extraction jobs
    t0 = time.time()
    results = _submit_parallel_jobs(app_prefixes, output)
    elapsed = round(time.time() - t0, 1)

    succeeded = sum(1 for r in results.values() if r["state"] == "SUCCESS")
    failed = len(results) - succeeded

    # Generate recommendations from extracted data
    recs = _generate_recommendations(output, mode)

    return json.dumps({
        "extraction": {"total": len(results), "succeeded": succeeded, "failed": failed, "elapsed_seconds": elapsed},
        "mode": mode,
        "application_count": len(recs),
        "recommendations": recs,
    }, indent=2)


def _generate_recommendations(output_path, mode):
    """Generate recommendations from extracted task_stage_summary JSONs."""
    apps = _list_apps()
    recs = []
    for app_id in apps:
        data = _load_app(app_id)
        if not data:
            continue
        io = data.get("io_summary", {}).get("application_level", {})
        ex = data.get("executor_summary", {})
        info = data.get("application_info", {})
        sd = data.get("shuffle_data_summary", {})
        recs.append({
            "application_id": app_id,
            "application_name": info.get("application_name", ""),
            "job_id": info.get("job_id", ""),
            "input_gb": round(io.get("total_input_gb", 0), 2),
            "shuffle_write_gb": round(io.get("total_shuffle_write_gb", 0), 2),
            "peak_shuffle_write_per_stage": sd.get("max_stage_shuffle_write_gb", 0),
            "duration_hours": data.get("total_run_duration_hours", 0),
            "avg_memory_utilization_percent": ex.get("avg_memory_utilization_percent", 0),
            "avg_cpu_utilization_percent": ex.get("avg_cpu_utilization_percent", 0),
            "idle_core_percentage": ex.get("idle_core_percentage", 0),
            "cost_factor": data.get("total_cost_factor", 0),
            "emr_serverless_storage_eligible": sd.get("emr_serverless_storage_eligible", False),
        })
    recs.sort(key=lambda r: r.get("cost_factor", 0), reverse=True)
    return recs


# ── Tool: Application Querying ────────────────────────────────────────

@mcp.tool()
def list_applications() -> str:
    """List all extracted Spark applications with summary metrics.

    Returns application IDs, names, durations, input sizes, and executor counts.
    Run analyze_spark_logs first to extract data.
    """
    apps = _list_apps()
    if not apps:
        return json.dumps({"error": "No extracted data found. Run analyze_spark_logs first."})

    summaries = []
    for app_id in apps:
        data = _load_app(app_id)
        if not data:
            continue
        info = data.get("application_info", {})
        io = data.get("io_summary", {}).get("application_level", {})
        ex = data.get("executor_summary", {})
        summaries.append({
            "application_id": app_id, "name": info.get("application_name", ""),
            "duration_hours": info.get("total_run_duration_hours"),
            "total_input_gb": io.get("total_input_gb"),
            "total_shuffle_write_gb": io.get("total_shuffle_write_gb"),
            "total_executors": ex.get("total_executors"),
            "active_executors": ex.get("active_executors"),
            "avg_cpu_util": ex.get("avg_cpu_utilization_percent"),
            "avg_mem_util": ex.get("avg_memory_utilization_percent"),
            "idle_core_percentage": ex.get("idle_core_percentage"),
            "cost_factor": data.get("total_cost_factor"),
        })
    summaries.sort(key=lambda x: x.get("cost_factor") or 0, reverse=True)
    return json.dumps({"application_count": len(summaries), "applications": summaries}, indent=2)


@mcp.tool()
def get_application(application_id: str) -> str:
    """Get detailed extracted metrics for a specific Spark application.

    Args:
        application_id: The application ID (with or without eventlog_v2_ prefix)
    """
    metrics = _load_app(application_id, "task_stage_summary")
    config = _load_app(application_id, "spark_config_extract")
    if not metrics:
        return json.dumps({"error": f"Application {application_id} not found. Run analyze_spark_logs first."})
    result = metrics.copy()
    if config:
        result["spark_configuration"] = config.get("spark_configuration", {})
    return json.dumps(result, indent=2)


@mcp.tool()
def list_event_log_prefixes(bucket: str, prefix: str = "") -> str:
    """List available Spark event log application prefixes in S3.

    Args:
        bucket: S3 bucket name
        prefix: S3 prefix to search under
    """
    apps = _discover_apps(f"s3://{bucket}/{prefix}")
    names = [a.split("/")[-2] for a in apps]
    return json.dumps(names, indent=2)


# ── Comparison Tools ──────────────────────────────────────────────────

@mcp.tool()
def compare_job_performance(app_id_1: str, app_id_2: str) -> str:
    """Compare performance metrics between two Spark applications.

    Args:
        app_id_1: First application ID
        app_id_2: Second application ID
    """
    d1, d2 = _load_app(app_id_1), _load_app(app_id_2)
    if not d1 or not d2:
        return json.dumps({"error": f"Application {app_id_1 if not d1 else app_id_2} not found."})

    def m(d):
        info, io = d.get("application_info", {}), d.get("io_summary", {}).get("application_level", {})
        ex, sp = d.get("executor_summary", {}), d.get("spill_summary", {})
        return {
            "duration_hours": info.get("total_run_duration_hours", 0),
            "total_tasks": d.get("task_summary", {}).get("total_tasks", 0),
            "total_input_gb": io.get("total_input_gb", 0),
            "total_shuffle_write_gb": io.get("total_shuffle_write_gb", 0),
            "total_executors": ex.get("total_executors", 0),
            "avg_cpu_util": ex.get("avg_cpu_utilization_percent", 0),
            "avg_mem_util": ex.get("avg_memory_utilization_percent", 0),
            "idle_core_pct": ex.get("idle_core_percentage", 0),
            "total_memory_spilled_gb": sp.get("total_memory_spilled_gb", 0),
            "total_disk_spilled_gb": sp.get("total_disk_spilled_gb", 0),
            "cost_factor": d.get("total_cost_factor", 0),
        }

    m1, m2 = m(d1), m(d2)
    comparison = {}
    for k in m1:
        v1, v2 = m1[k] or 0, m2[k] or 0
        delta_pct = round((v2 - v1) / v1 * 100, 1) if v1 else None
        comparison[k] = {app_id_1: v1, app_id_2: v2, "delta_pct": delta_pct}
    return json.dumps({"comparison": comparison}, indent=2)


@mcp.tool()
def compare_job_environments(app_id_1: str, app_id_2: str) -> str:
    """Compare Spark configurations between two applications.

    Args:
        app_id_1: First application ID
        app_id_2: Second application ID
    """
    c1, c2 = _load_app(app_id_1, "spark_config_extract"), _load_app(app_id_2, "spark_config_extract")
    if not c1 or not c2:
        return json.dumps({"error": f"Config for {app_id_1 if not c1 else app_id_2} not found."})

    cfg1, cfg2 = c1.get("spark_configuration", {}), c2.get("spark_configuration", {})
    skip = {"spark.app.id", "spark.app.startTime", "spark.app.submitTime",
            "spark.app.initial.jar.urls", "spark.app.initial.file.urls",
            "spark.app.initial.archive.urls", "spark.driver.host", "spark.driver.port"}

    all_keys = sorted((set(cfg1) | set(cfg2)) - skip)
    different, only_1, only_2 = {}, {}, {}
    for k in all_keys:
        if k in cfg1 and k in cfg2:
            if str(cfg1[k]) != str(cfg2[k]):
                different[k] = {app_id_1: cfg1[k], app_id_2: cfg2[k]}
        elif k in cfg1:
            only_1[k] = cfg1[k]
        else:
            only_2[k] = cfg2[k]

    return json.dumps({"different": different, f"only_in_{app_id_1}": only_1,
        f"only_in_{app_id_2}": only_2, "summary": {"total": len(all_keys),
        "different": len(different), f"only_in_{app_id_1}": len(only_1),
        f"only_in_{app_id_2}": len(only_2)}}, indent=2)


# ── Bottleneck Analysis ───────────────────────────────────────────────

@mcp.tool()
def get_bottlenecks(application_id: str) -> str:
    """Identify performance bottlenecks with actionable recommendations.

    Analyzes executor utilization, memory pressure, spill, shuffle, idle waste, and stage issues.

    Args:
        application_id: The application ID
    """
    data = _load_app(application_id)
    if not data:
        return json.dumps({"error": f"Application {application_id} not found."})

    io = data.get("io_summary", {}).get("application_level", {})
    ex = data.get("executor_summary", {})
    sp = data.get("spill_summary", {})
    sd = data.get("shuffle_data_summary", {})
    findings = []

    cpu = ex.get("avg_cpu_utilization_percent", 0) or 0
    if cpu < 20:
        findings.append({"severity": "HIGH", "category": "CPU", "finding": f"Very low CPU utilization ({cpu}%)",
            "recommendation": "Reduce executor cores or count — app is over-provisioned."})
    elif cpu < 40:
        findings.append({"severity": "MEDIUM", "category": "CPU", "finding": f"Low CPU utilization ({cpu}%)",
            "recommendation": "Consider reducing executor cores."})

    mem = ex.get("avg_memory_utilization_percent", 0) or 0
    if mem > 85:
        findings.append({"severity": "HIGH", "category": "Memory", "finding": f"High memory utilization ({mem}%)",
            "recommendation": "Increase executor memory or reduce partition size."})
    elif mem < 30:
        findings.append({"severity": "MEDIUM", "category": "Memory", "finding": f"Low memory utilization ({mem}%)",
            "recommendation": "Reduce executor memory to save cost."})

    idle_core = ex.get("idle_core_percentage", 0) or 0
    if idle_core > 60:
        findings.append({"severity": "HIGH", "category": "Cores", "finding": f"Idle core percentage is {idle_core}%",
            "recommendation": "Most core-hours are wasted. Reduce total cores or improve parallelism."})

    total_exec = ex.get("total_executors", 0) or 0
    active_exec = ex.get("active_executors", 0) or 0
    if total_exec > 0 and (total_exec - active_exec) / total_exec > 0.3:
        findings.append({"severity": "HIGH", "category": "Executors",
            "finding": f"{total_exec - active_exec}/{total_exec} executors never ran tasks",
            "recommendation": "Tune spark.dynamicAllocation.maxExecutors."})

    disk_spill = sp.get("total_disk_spilled_gb", 0) or 0
    mem_spill = sp.get("total_memory_spilled_gb", 0) or 0
    if disk_spill > 0:
        findings.append({"severity": "HIGH" if disk_spill > 100 else "MEDIUM", "category": "Spill",
            "finding": f"Disk spill: {disk_spill} GB, Memory spill: {mem_spill} GB",
            "recommendation": "Increase executor memory or shuffle partitions."})

    peak_shuf = sd.get("max_stage_shuffle_write_gb", 0) or 0
    if peak_shuf > 200:
        findings.append({"severity": "HIGH", "category": "Shuffle Storage",
            "finding": f"Peak stage shuffle write: {peak_shuf} GB (exceeds 200GB Serverless storage limit)",
            "recommendation": "Not eligible for EMR Serverless managed storage. Use shuffle-optimized disks."})

    stages = data.get("stage_summary", {}).get("stages", [])
    if stages:
        total_time = sum(s.get("duration_sec", 0) for s in stages)
        if total_time > 0:
            slowest = max(stages, key=lambda s: s.get("duration_sec", 0))
            pct = round(slowest["duration_sec"] / total_time * 100, 1)
            if pct > 50:
                findings.append({"severity": "HIGH", "category": "Stage",
                    "finding": f"Stage {slowest['stage_id']} takes {pct}% of total time ({slowest['duration_sec']}s)",
                    "recommendation": "Focus optimization on this dominant stage."})

        failed = [s for s in stages if s.get("failure_reason")]
        if failed:
            findings.append({"severity": "HIGH", "category": "Failures",
                "finding": f"{len(failed)} stages failed",
                "recommendation": "; ".join(f"stage {s['stage_id']}: {(s['failure_reason'] or '')[:80]}" for s in failed[:3])})

    findings.sort(key=lambda f: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[f["severity"]])
    return json.dumps({"application_id": application_id, "bottleneck_count": len(findings), "findings": findings}, indent=2)


# ── Stage Analysis ────────────────────────────────────────────────────

@mcp.tool()
def list_slowest_stages(application_id: str, top_n: int = 10) -> str:
    """Get the N slowest stages sorted by duration with IO/shuffle/spill metrics.

    Args:
        application_id: The application ID
        top_n: Number of stages to return (default 10)
    """
    data = _load_app(application_id)
    if not data:
        return json.dumps({"error": f"Application {application_id} not found."})
    stages = sorted(data.get("stage_summary", {}).get("stages", []),
                    key=lambda s: s.get("duration_sec", 0), reverse=True)[:top_n]
    return json.dumps({"application_id": application_id, "slowest_stages": stages}, indent=2)


@mcp.tool()
def get_stage_details(application_id: str, stage_id: int) -> str:
    """Get detailed metrics for a specific stage.

    Args:
        application_id: The application ID
        stage_id: The stage ID number
    """
    data = _load_app(application_id)
    if not data:
        return json.dumps({"error": f"Application {application_id} not found."})
    for s in data.get("stage_summary", {}).get("stages", []):
        if s["stage_id"] == stage_id:
            return json.dumps(s, indent=2)
    return json.dumps({"error": f"Stage {stage_id} not found."})


# ── Resource Timeline ─────────────────────────────────────────────────

@mcp.tool()
def get_resource_timeline(application_id: str) -> str:
    """Get executor add/remove events showing scaling behavior over time.

    Args:
        application_id: The application ID
    """
    data = _load_app(application_id)
    if not data:
        return json.dumps({"error": f"Application {application_id} not found."})
    timeline = data.get("executor_timeline", [])
    if not timeline:
        return json.dumps({"error": "No timeline data."})
    running, peak, annotated = 0, 0, []
    base_ts = timeline[0]["time_ms"]
    for evt in timeline:
        running += 1 if evt["event"] == "added" else -1
        peak = max(peak, running)
        annotated.append({**evt, "active_count": running, "relative_sec": round((evt["time_ms"] - base_ts) / 1000, 1)})
    return json.dumps({"application_id": application_id, "peak_executors": peak, "timeline": annotated}, indent=2)


# ── SQL Plan Analysis ─────────────────────────────────────────────────

@mcp.tool()
def list_sql_executions(application_id: str) -> str:
    """List SQL executions with duration and description.

    Args:
        application_id: The application ID
    """
    data = _load_app(application_id)
    if not data:
        return json.dumps({"error": f"Application {application_id} not found."})
    sqls = data.get("sql_executions", data.get("sql_metrics", {}).get("sql_executions", []))
    if not sqls:
        return json.dumps({"error": "No SQL data."})
    summary = [{"execution_id": s.get("execution_id"), "description": s.get("description", ""),
                "duration_sec": s.get("duration_sec", s.get("duration_ms", 0) / 1000 if s.get("duration_ms") else 0)}
               for s in sqls]
    return json.dumps({"application_id": application_id, "sql_executions": summary}, indent=2)


@mcp.tool()
def compare_sql_execution_plans(app_id_1: str, sql_id_1: int, app_id_2: str, sql_id_2: int) -> str:
    """Compare SQL execution plans between two queries.

    Args:
        app_id_1: First application ID
        sql_id_1: SQL execution ID in first app
        app_id_2: Second application ID
        sql_id_2: SQL execution ID in second app
    """
    d1, d2 = _load_app(app_id_1), _load_app(app_id_2)
    if not d1 or not d2:
        return json.dumps({"error": f"Application {app_id_1 if not d1 else app_id_2} not found."})

    def find(data, sid):
        for s in data.get("sql_executions", data.get("sql_metrics", {}).get("sql_executions", [])):
            if s.get("execution_id") == sid:
                return s
        return None

    s1, s2 = find(d1, sql_id_1), find(d2, sql_id_2)
    if not s1 or not s2:
        return json.dumps({"error": f"SQL execution not found."})

    plan1 = s1.get("physical_plan", s1.get("physical_plan_description", ""))
    plan2 = s2.get("physical_plan", s2.get("physical_plan_description", ""))

    def ops(plan):
        return [l.lstrip("+- ").lstrip(":").split("(")[0].strip() for l in plan.split("\n") if l.strip() and not l.strip().startswith("==")]

    o1, o2 = ops(plan1), ops(plan2)
    return json.dumps({
        "query_1": {"app": app_id_1, "sql_id": sql_id_1, "description": s1.get("description", ""),
                     "plan": plan1[:5000]},
        "query_2": {"app": app_id_2, "sql_id": sql_id_2, "description": s2.get("description", ""),
                     "plan": plan2[:5000]},
        "operator_diff": {"unique_to_1": list(set(o1) - set(o2))[:20], "unique_to_2": list(set(o2) - set(o1))[:20]},
    }, indent=2)


# ── EMR Serverless Configuration Recommendations ───────────────────

@mcp.tool()
def generate_emr_serverless_config_recommendations(
    mode: str = "cost-optimized",
    target_partition_size_mib: int = 1024,
) -> str:
    """Generate EMR Serverless configuration recommendations with worker sizing, Spark configs, and shuffle tuning.

    Uses extracted metrics (run analyze_spark_logs first) to produce ready-to-use
    Spark configurations including executor/driver sizing, shuffle partitions,
    dynamic allocation limits, disk sizing, and bottleneck warnings.

    Args:
        mode: 'cost-optimized' or 'performance-optimized'
        target_partition_size_mib: Target shuffle partition size in MiB (default 1024)

    Returns:
        JSON with per-application recommendations including spark_configs, worker sizing, and shuffle tuning.
    """
    from emr_recommender import generate_dual_recommendations

    output = _output_path()
    if not output:
        return json.dumps({"error": "OUTPUT_S3_PATH not configured."})

    try:
        cost_recs, perf_recs = generate_dual_recommendations(
            output, target_partition_size_mib=target_partition_size_mib
        )
    except Exception as e:
        return json.dumps({"error": f"Recommendation generation failed: {e}"})

    recs = cost_recs if mode == "cost-optimized" else perf_recs
    if not recs:
        return json.dumps({"error": "No extracted data found. Run analyze_spark_logs first."})

    return json.dumps({
        "mode": mode,
        "application_count": len(recs),
        "recommendations": recs,
    }, indent=2)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
