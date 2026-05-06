#!/usr/bin/env python3
"""
EMR Serverless Spark Config Advisor MCP Server

Analyzes Spark event logs from S3, extracts metrics, generates optimized
EMR Serverless configurations, and provides interactive querying, comparison,
and bottleneck analysis through 12 MCP tools.

Configuration via environment variables:
    EC2_HOST: SSH target (e.g. hadoop@ec2-1-2-3-4.compute-1.amazonaws.com)
    SSH_KEY:  Path to SSH private key (e.g. ~/.ssh/my-key.pem)
"""

import json
import os
import subprocess
import time
from mcp.server.fastmcp import FastMCP

EC2_HOST = os.environ.get("EC2_HOST", "hadoop@localhost")
SSH_KEY = os.environ.get("SSH_KEY", "~/.ssh/id_rsa")
SSH_CMD = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=10 {EC2_HOST}"
EXTRACT_DIR = "/tmp/spark_advisor_output"

mcp = FastMCP("spark-config-advisor")


def _ssh(cmd, timeout=900):
    """Run command on EC2 via SSH."""
    result = subprocess.run(
        f'{SSH_CMD} \'{cmd}\'',
        shell=True, capture_output=True, text=True, timeout=timeout
    )
    return result.stdout, result.stderr, result.returncode


def _load_app_json(app_id, subdir="task_stage_summary"):
    """Load extracted JSON for an app from EC2."""
    for name in [app_id, f"eventlog_v2_{app_id}"]:
        out, _, rc = _ssh(f"cat {EXTRACT_DIR}/{subdir}/{name}.json 2>/dev/null")
        if rc == 0 and out.strip():
            return json.loads(out)
    return None


def _list_extracted_apps():
    """List all extracted app IDs."""
    out, _, rc = _ssh(f"ls {EXTRACT_DIR}/task_stage_summary/ 2>/dev/null")
    if rc != 0:
        return []
    return [f.replace("eventlog_v2_", "").replace(".json", "")
            for f in out.strip().split("\n") if f.endswith(".json")]


# ── Extraction & Recommendations ─────────────────────────────────────

@mcp.tool()
def analyze_spark_logs(
    input_path: str,
    mode: str = "cost-optimized",
    limit: int = 100,
) -> str:
    """Analyze Spark event logs from S3 and generate EMR Serverless configuration recommendations.

    Extracts metrics and generates optimized Spark configs. Results are cached
    for interactive querying with other tools (get_application, compare_*, get_bottlenecks).

    Args:
        input_path: S3 path to event logs (e.g. s3://bucket/prefix/)
        mode: 'cost-optimized' or 'performance-optimized'
        limit: Max applications to analyze (default 100)

    Returns:
        JSON with recommendations per application.
    """
    if not input_path.startswith("s3://"):
        return json.dumps({"error": "input_path must be an S3 path (s3://bucket/prefix/)"})

    parts = input_path.replace("s3://", "").split("/", 1)
    input_bucket = parts[0]
    input_prefix = parts[1] if len(parts) > 1 else ""

    run_id = str(int(time.time()))
    staging_prefix = f"mcp-staging/{run_id}/"
    output_file = f"/tmp/recs_{run_id}.json"

    pipeline_cmd = (
        f"cd ~ && python3 pipeline_wrapper.py"
        f" --input-bucket {input_bucket}"
        f" --input-prefix {input_prefix}"
        f" --staging-prefix {staging_prefix}"
        f" --output-path {EXTRACT_DIR}"
        f" --output {output_file}"
        f" --limit {limit}"
    )
    if mode == "cost-optimized":
        pipeline_cmd += " --cost-optimized"
    elif mode == "performance-optimized":
        pipeline_cmd += " --performance-optimized"

    stdout, stderr, rc = _ssh(pipeline_cmd)
    if rc != 0:
        return json.dumps({"error": f"Pipeline failed (exit {rc})", "stderr": stderr[-2000:], "stdout": stdout[-2000:]})

    cost_file = output_file.replace(".json", "_cost.json")
    perf_file = output_file.replace(".json", "_perf.json")
    target = cost_file if mode == "cost-optimized" else perf_file

    out, err, rc = _ssh(f"cat {target} 2>/dev/null || cat {cost_file} 2>/dev/null || cat {perf_file}")
    if rc != 0:
        return json.dumps({"error": "Could not read results", "stderr": err})

    try:
        results = json.loads(out)
        return json.dumps({"mode": mode, "application_count": len(results), "recommendations": results}, indent=2)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in results", "raw": out[:2000]})


# ── Application Querying ─────────────────────────────────────────────

@mcp.tool()
def list_applications() -> str:
    """List all extracted Spark applications with summary metrics.

    Returns application IDs, names, durations, input sizes, and executor counts.
    Run analyze_spark_logs first to extract data.
    """
    apps = _list_extracted_apps()
    if not apps:
        return json.dumps({"error": "No extracted data found. Run analyze_spark_logs first."})

    summaries = []
    for app_id in apps:
        data = _load_app_json(app_id)
        if not data:
            continue
        info = data.get("application_info", {})
        io = data.get("io_summary", {}).get("application_level", {})
        ex = data.get("executor_summary", {})
        summaries.append({
            "application_id": app_id,
            "name": info.get("application_name", ""),
            "duration_hours": info.get("total_run_duration_hours"),
            "total_input_gb": io.get("total_input_gb"),
            "total_shuffle_write_gb": io.get("total_shuffle_write_gb"),
            "total_executors": ex.get("total_executors"),
            "active_executors": ex.get("active_executors"),
            "avg_cpu_util": ex.get("avg_cpu_utilization_percent"),
            "avg_mem_util": ex.get("avg_memory_utilization_percent"),
            "cost_factor": data.get("total_cost_factor"),
        })

    summaries.sort(key=lambda x: x.get("cost_factor") or 0, reverse=True)
    return json.dumps({"application_count": len(summaries), "applications": summaries}, indent=2)


@mcp.tool()
def get_application(application_id: str) -> str:
    """Get detailed extracted metrics for a specific Spark application.

    Returns executor summary, IO metrics, spill data, config, and application info.

    Args:
        application_id: The application ID (with or without eventlog_v2_ prefix)
    """
    metrics = _load_app_json(application_id, "task_stage_summary")
    config = _load_app_json(application_id, "spark_config_extract")
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
    out, _, rc = _ssh(f"aws s3 ls s3://{bucket}/{prefix} --region us-east-1")
    if rc != 0:
        return json.dumps({"error": "Failed to list S3"})
    prefixes = [line.strip().split()[-1] for line in out.strip().split("\n") if line.strip().startswith("PRE")]
    return json.dumps(prefixes, indent=2)


# ── Comparison Tools ─────────────────────────────────────────────────

@mcp.tool()
def compare_job_performance(app_id_1: str, app_id_2: str) -> str:
    """Compare performance metrics between two Spark applications.

    Shows side-by-side comparison of duration, IO, executors, CPU/memory utilization,
    spill, and cost factor with percentage deltas.

    Args:
        app_id_1: First application ID
        app_id_2: Second application ID
    """
    d1 = _load_app_json(app_id_1)
    d2 = _load_app_json(app_id_2)
    if not d1 or not d2:
        missing = app_id_1 if not d1 else app_id_2
        return json.dumps({"error": f"Application {missing} not found."})

    def extract_metrics(d):
        info = d.get("application_info", {})
        io = d.get("io_summary", {}).get("application_level", {})
        ex = d.get("executor_summary", {})
        sp = d.get("spill_summary", {})
        return {
            "duration_hours": info.get("total_run_duration_hours", 0),
            "total_tasks": d.get("task_summary", {}).get("total_tasks", 0),
            "total_input_gb": io.get("total_input_gb", 0),
            "total_output_gb": io.get("total_output_gb", 0),
            "total_shuffle_read_gb": io.get("total_shuffle_read_gb", 0),
            "total_shuffle_write_gb": io.get("total_shuffle_write_gb", 0),
            "total_executors": ex.get("total_executors", 0),
            "active_executors": ex.get("active_executors", 0),
            "total_cores": ex.get("total_cores", 0),
            "avg_cpu_util": ex.get("avg_cpu_utilization_percent", 0),
            "avg_mem_util": ex.get("avg_memory_utilization_percent", 0),
            "idle_core_pct": ex.get("idle_core_percentage", 0),
            "max_peak_memory_gb": ex.get("max_peak_memory_gb", 0),
            "total_memory_spilled_gb": sp.get("total_memory_spilled_gb", 0),
            "total_disk_spilled_gb": sp.get("total_disk_spilled_gb", 0),
            "cost_factor": d.get("total_cost_factor", 0),
        }

    m1, m2 = extract_metrics(d1), extract_metrics(d2)
    comparison = {}
    for key in m1:
        v1, v2 = m1[key] or 0, m2[key] or 0
        delta_pct = round((v2 - v1) / v1 * 100, 1) if v1 else None
        comparison[key] = {app_id_1: v1, app_id_2: v2, "delta_pct": delta_pct}

    return json.dumps({"comparison": comparison}, indent=2)


@mcp.tool()
def compare_job_environments(app_id_1: str, app_id_2: str) -> str:
    """Compare Spark configurations between two applications.

    Shows configs that differ, and configs unique to each app.

    Args:
        app_id_1: First application ID
        app_id_2: Second application ID
    """
    c1 = _load_app_json(app_id_1, "spark_config_extract")
    c2 = _load_app_json(app_id_2, "spark_config_extract")
    if not c1 or not c2:
        missing = app_id_1 if not c1 else app_id_2
        return json.dumps({"error": f"Config for {missing} not found."})

    cfg1 = c1.get("spark_configuration", {})
    cfg2 = c2.get("spark_configuration", {})

    skip = {"spark.app.id", "spark.app.startTime", "spark.app.submitTime",
            "spark.app.initial.jar.urls", "spark.app.initial.file.urls",
            "spark.app.initial.archive.urls", "spark.driver.host", "spark.driver.port"}

    all_keys = sorted((set(cfg1) | set(cfg2)) - skip)
    different, only_1, only_2 = {}, {}, {}

    for k in all_keys:
        in1, in2 = k in cfg1, k in cfg2
        if in1 and in2:
            if str(cfg1[k]) != str(cfg2[k]):
                different[k] = {app_id_1: cfg1[k], app_id_2: cfg2[k]}
        elif in1:
            only_1[k] = cfg1[k]
        else:
            only_2[k] = cfg2[k]

    return json.dumps({
        "different": different,
        f"only_in_{app_id_1}": only_1,
        f"only_in_{app_id_2}": only_2,
        "summary": {
            "total_configs_compared": len(all_keys),
            "different": len(different),
            f"only_in_{app_id_1}": len(only_1),
            f"only_in_{app_id_2}": len(only_2),
        }
    }, indent=2)


# ── Bottleneck Analysis ──────────────────────────────────────────────

@mcp.tool()
def get_bottlenecks(application_id: str) -> str:
    """Identify performance bottlenecks for a Spark application with actionable recommendations.

    Analyzes executor utilization, memory pressure, spill, shuffle, data skew indicators,
    idle resource waste, and stage-level issues. Returns prioritized findings with severity levels.

    Args:
        application_id: The application ID
    """
    data = _load_app_json(application_id)
    if not data:
        return json.dumps({"error": f"Application {application_id} not found."})

    io = data.get("io_summary", {}).get("application_level", {})
    ex = data.get("executor_summary", {})
    sp = data.get("spill_summary", {})
    info = data.get("application_info", {})
    findings = []

    cpu = ex.get("avg_cpu_utilization_percent", 0) or 0
    if cpu < 20:
        findings.append({"severity": "HIGH", "category": "CPU",
            "finding": f"Very low CPU utilization ({cpu}%)",
            "recommendation": "Reduce executor cores or executor count. App is over-provisioned for compute."})
    elif cpu < 40:
        findings.append({"severity": "MEDIUM", "category": "CPU",
            "finding": f"Low CPU utilization ({cpu}%)",
            "recommendation": "Consider reducing executor cores. Some over-provisioning detected."})

    mem = ex.get("avg_memory_utilization_percent", 0) or 0
    peak = ex.get("max_peak_memory_gb", 0) or 0
    if mem > 85:
        findings.append({"severity": "HIGH", "category": "Memory",
            "finding": f"High memory utilization ({mem}%, peak {peak} GB)",
            "recommendation": "Increase executor memory or reduce partition size to avoid OOM."})
    elif mem < 30:
        findings.append({"severity": "MEDIUM", "category": "Memory",
            "finding": f"Low memory utilization ({mem}%, peak {peak} GB)",
            "recommendation": "Reduce executor memory to save cost."})

    total_exec = ex.get("total_executors", 0) or 0
    active_exec = ex.get("active_executors", 0) or 0
    idle_exec = total_exec - active_exec
    if idle_exec > 0 and total_exec > 0:
        idle_pct = round(idle_exec / total_exec * 100, 1)
        sev = "HIGH" if idle_pct > 30 else "MEDIUM"
        findings.append({"severity": sev, "category": "Executors",
            "finding": f"{idle_exec}/{total_exec} executors ({idle_pct}%) were allocated but never ran tasks",
            "recommendation": "Tune spark.dynamicAllocation.maxExecutors or increase minExecutors idle timeout."})

    idle_core = ex.get("idle_core_percentage", 0) or 0
    if idle_core > 80:
        findings.append({"severity": "HIGH", "category": "Cores",
            "finding": f"Idle core percentage is {idle_core}%",
            "recommendation": "Most core-hours are wasted. Reduce total cores or improve parallelism."})

    mem_spill = sp.get("total_memory_spilled_gb", 0) or 0
    disk_spill = sp.get("total_disk_spilled_gb", 0) or 0
    total_input = io.get("total_input_gb", 0) or 0
    if disk_spill > 0:
        sev = "HIGH" if total_input == 0 or disk_spill > total_input * 0.1 else "MEDIUM"
        findings.append({"severity": sev, "category": "Spill",
            "finding": f"Disk spill: {disk_spill} GB, Memory spill: {mem_spill} GB",
            "recommendation": "Increase executor memory or spark.sql.shuffle.partitions to reduce spill."})

    shuffle_write = io.get("total_shuffle_write_gb", 0) or 0
    if total_input > 0 and shuffle_write > total_input * 0.5:
        ratio = round(shuffle_write / total_input * 100, 1)
        findings.append({"severity": "MEDIUM", "category": "Shuffle",
            "finding": f"Shuffle write is {ratio}% of input ({shuffle_write} GB shuffle vs {total_input} GB input)",
            "recommendation": "Consider broadcast joins for small tables, or increase shuffle partitions."})

    duration = info.get("total_run_duration_hours", 0) or 0
    if duration < 0.1 and total_exec > 50:
        findings.append({"severity": "MEDIUM", "category": "Provisioning",
            "finding": f"Short job ({round(duration*60, 1)} min) with {total_exec} executors",
            "recommendation": "Job finishes quickly — reduce max executors to avoid allocation overhead."})

    stages = data.get("stage_summary", {}).get("stages", [])
    if stages:
        total_stage_time = sum(s.get("duration_sec", 0) for s in stages)
        if total_stage_time > 0:
            slowest = max(stages, key=lambda s: s.get("duration_sec", 0))
            slowest_pct = round(slowest["duration_sec"] / total_stage_time * 100, 1)
            if slowest_pct > 50:
                findings.append({"severity": "HIGH", "category": "Stage",
                    "finding": f"Stage {slowest['stage_id']} takes {slowest_pct}% of total time ({slowest['duration_sec']}s): {slowest.get('name', '')[:60]}",
                    "recommendation": "Focus optimization on this dominant stage."})

        spill_stages = [s for s in stages if s.get("disk_spill_gb", 0) > 0]
        if spill_stages:
            worst = max(spill_stages, key=lambda s: s["disk_spill_gb"])
            findings.append({"severity": "MEDIUM", "category": "Stage Spill",
                "finding": f"{len(spill_stages)} stages have disk spill. Worst: stage {worst['stage_id']} ({worst['disk_spill_gb']} GB)",
                "recommendation": f"Increase memory or partitions for stage {worst['stage_id']}."})

        failed = [s for s in stages if s.get("failure_reason")]
        if failed:
            findings.append({"severity": "HIGH", "category": "Failures",
                "finding": f"{len(failed)} stages failed",
                "recommendation": "Check failure reasons: " + "; ".join(
                    f"stage {s['stage_id']}: {(s['failure_reason'] or '')[:80]}" for s in failed[:3])})

    findings.sort(key=lambda f: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(f["severity"], 3))
    return json.dumps({"application_id": application_id, "bottleneck_count": len(findings), "findings": findings}, indent=2)


# ── Stage Analysis ───────────────────────────────────────────────────

@mcp.tool()
def list_slowest_stages(application_id: str, top_n: int = 10) -> str:
    """Get the N slowest stages for a Spark application, sorted by duration.

    Shows per-stage duration, task count, IO, shuffle, and spill metrics.

    Args:
        application_id: The application ID
        top_n: Number of slowest stages to return (default 10)
    """
    data = _load_app_json(application_id)
    if not data:
        return json.dumps({"error": f"Application {application_id} not found."})

    stages = data.get("stage_summary", {}).get("stages", [])
    if not stages:
        return json.dumps({"error": "No stage data. Re-run analyze_spark_logs to extract stage details."})

    stages_sorted = sorted(stages, key=lambda s: s.get("duration_sec", 0), reverse=True)[:top_n]
    return json.dumps({"application_id": application_id, "slowest_stages": stages_sorted}, indent=2)


@mcp.tool()
def get_stage_details(application_id: str, stage_id: int) -> str:
    """Get detailed metrics for a specific stage.

    Args:
        application_id: The application ID
        stage_id: The stage ID number
    """
    data = _load_app_json(application_id)
    if not data:
        return json.dumps({"error": f"Application {application_id} not found."})

    for s in data.get("stage_summary", {}).get("stages", []):
        if s["stage_id"] == stage_id:
            return json.dumps(s, indent=2)
    return json.dumps({"error": f"Stage {stage_id} not found."})


# ── Resource Timeline ────────────────────────────────────────────────

@mcp.tool()
def get_resource_timeline(application_id: str) -> str:
    """Get chronological executor add/remove events showing resource allocation over time.

    Useful for understanding scaling behavior and identifying over/under-provisioning periods.

    Args:
        application_id: The application ID
    """
    data = _load_app_json(application_id)
    if not data:
        return json.dumps({"error": f"Application {application_id} not found."})

    timeline = data.get("executor_timeline", [])
    if not timeline:
        return json.dumps({"error": "No timeline data. Re-run analyze_spark_logs to extract timeline."})

    running = 0
    peak = 0
    annotated = []
    for evt in timeline:
        running += 1 if evt["event"] == "added" else -1
        peak = max(peak, running)
        annotated.append({**evt, "active_count": running})

    if annotated:
        base_ts = annotated[0]["time_ms"]
        for evt in annotated:
            evt["relative_sec"] = round((evt["time_ms"] - base_ts) / 1000, 1)

    return json.dumps({"application_id": application_id, "total_events": len(annotated),
                        "peak_executors": peak, "timeline": annotated}, indent=2)


# ── SQL Plan Analysis ────────────────────────────────────────────────

@mcp.tool()
def list_sql_executions(application_id: str) -> str:
    """List all SQL executions for a Spark application with duration and description.

    Args:
        application_id: The application ID
    """
    data = _load_app_json(application_id)
    if not data:
        return json.dumps({"error": f"Application {application_id} not found."})

    sqls = data.get("sql_executions", [])
    if not sqls:
        return json.dumps({"error": "No SQL data. App may not use Spark SQL."})

    summary = [{"execution_id": s["execution_id"], "description": s["description"],
                "duration_sec": s["duration_sec"],
                "plan_length": len(s.get("physical_plan", ""))} for s in sqls]
    return json.dumps({"application_id": application_id, "sql_executions": summary}, indent=2)


@mcp.tool()
def compare_sql_execution_plans(
    app_id_1: str, sql_id_1: int,
    app_id_2: str, sql_id_2: int,
) -> str:
    """Compare SQL execution plans between two Spark SQL queries.

    Can compare across different applications or within the same app.

    Args:
        app_id_1: First application ID
        sql_id_1: SQL execution ID in first app
        app_id_2: Second application ID
        sql_id_2: SQL execution ID in second app
    """
    d1 = _load_app_json(app_id_1)
    d2 = _load_app_json(app_id_2)
    if not d1 or not d2:
        missing = app_id_1 if not d1 else app_id_2
        return json.dumps({"error": f"Application {missing} not found."})

    def find_sql(data, sql_id):
        for s in data.get("sql_executions", []):
            if s["execution_id"] == sql_id:
                return s
        return None

    s1, s2 = find_sql(d1, sql_id_1), find_sql(d2, sql_id_2)
    if not s1 or not s2:
        missing = f"{app_id_1}/sql_{sql_id_1}" if not s1 else f"{app_id_2}/sql_{sql_id_2}"
        return json.dumps({"error": f"SQL execution {missing} not found."})

    def extract_operators(plan_text):
        return [line.lstrip("+- ").lstrip(":").split("(")[0].strip()
                for line in plan_text.split("\n")
                if line.strip() and not line.strip().startswith("==")]

    ops1, ops2 = extract_operators(s1.get("physical_plan", "")), extract_operators(s2.get("physical_plan", ""))

    return json.dumps({
        "query_1": {"app": app_id_1, "sql_id": sql_id_1, "description": s1["description"],
                     "duration_sec": s1["duration_sec"], "plan": s1.get("physical_plan", "")[:5000]},
        "query_2": {"app": app_id_2, "sql_id": sql_id_2, "description": s2["description"],
                     "duration_sec": s2["duration_sec"], "plan": s2.get("physical_plan", "")[:5000]},
        "operator_diff": {
            "operators_in_1": len(ops1), "operators_in_2": len(ops2),
            "unique_to_1": list(set(ops1) - set(ops2))[:20],
            "unique_to_2": list(set(ops2) - set(ops1))[:20],
        }
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
