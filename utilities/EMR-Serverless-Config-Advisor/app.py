#!/usr/bin/env python3
"""
EMR Serverless Advisor — FastAPI Web UI
=======================================
Landing page navigates to:
  - Config Advisor: upload extracted event log JSON (task_stage_summary output
    from spark_extractor.py) for bottleneck classification, worker sizing, and
    Spark config recommendations
  - Observability: live driver/executor metrics (heap, RSS, CPU, GC) from
    Prometheus for metrics-enabled job runs

Run:
    screen -S emr-serverless-advisor python3 app.py
API docs (auto-generated): http://localhost:5000/docs
"""

import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Import the fine tuner for recommendations
from emr_s_fine_tuner import generate_dual_recommendations

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="EMR Serverless Advisor")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _asset_v(name: str) -> int:
    """Cache-busting version for static assets: the file's mtime, so any
    edit to a stylesheet invalidates browser caches automatically."""
    try:
        return int((BASE_DIR / "static" / name).stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["asset_v"] = _asset_v

UPLOAD_DIR = Path(tempfile.gettempdir()) / "emr_config_advisor_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500MB max upload

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Newest EMR release — drives the Upgrade action on the home page
EMR_LATEST_RELEASE = os.environ.get("EMR_LATEST_RELEASE", "emr-8.0.0")

# EMR Serverless billing rates (us-east-1 x86 defaults; override per region)
EMRS_PRICE = {
    "vcpu_hour": float(os.environ.get("EMRS_PRICE_VCPU_HOUR", "0.052624")),
    "gb_hour": float(os.environ.get("EMRS_PRICE_GB_HOUR", "0.0057785")),
    "storage_gb_hour": float(os.environ.get("EMRS_PRICE_STORAGE_GB_HOUR", "0.000111")),
}


def _emrs_cost_usd(vcpu_hours: float, gb_hours: float,
                   storage_gb_hours: float = 0.0) -> float:
    return round(vcpu_hours * EMRS_PRICE["vcpu_hour"]
                 + gb_hours * EMRS_PRICE["gb_hour"]
                 + storage_gb_hours * EMRS_PRICE["storage_gb_hour"], 4)


def _parse_mem_gb(v: str) -> float:
    m = re.match(r"([\d.]+)\s*([gmt]?)", str(v).lower())
    if not m:
        return 0.0
    n, unit = float(m.group(1)), m.group(2)
    return n / 1024 if unit == "m" else n * 1024 if unit == "t" else n


def _run_cost(result: dict) -> dict | None:
    """EMR-S billing units for an analyzed run.

    Prefers billedResourceUtilization from the EMR API (spark.app.id in the
    event log IS the job run id) — exact, matches the console. Otherwise
    estimates from the event log: per-executor add→remove lifetimes ×
    (heap+overhead rounded up to whole GB). Estimates run LOW (~10-25%)
    because billing includes worker provisioning/teardown time that never
    appears in the event log; validated -19% vs billed on a known run.
    """
    cfg = result.get("spark_config_source") or {}
    app_id = cfg.get("spark.app.id", "")
    if _JOB_RUN_ID_RE.match(app_id):
        try:
            emr_app = _find_application_for_job(app_id)
            if emr_app:
                jr = _emr().get_job_run(applicationId=emr_app,
                                        jobRunId=app_id)["jobRun"]
                b = jr.get("billedResourceUtilization")
                if b:
                    vcpu, mem = b.get("vCPUHour", 0), b.get("memoryGBHour", 0)
                    sto = b.get("storageGBHour", 0)
                    return {"source": "billed", "vcpu_hours": vcpu,
                            "memory_gb_hours": mem, "storage_gb_hours": sto,
                            "cost_usd": _emrs_cost_usd(vcpu, mem, sto)}
        except Exception:
            pass

    dur = result.get("duration_hours") or 0
    execs = result.get("metrics_summary", {}).get("total_executors", 0)
    if not dur or not execs:
        return None
    is_ec2_source = bool(cfg.get("spark.emr_cluster_id"))
    exec_cores = int(cfg.get("spark.executor.cores", 4))
    drv_cores = int(cfg.get("spark.driver.cores", 4))
    # EMR Serverless bills the worker memory = executor heap + overhead
    # (memoryOverheadFactor, default 0.1)
    overhead = 1 + float(cfg.get("spark.emr-serverless.memoryOverheadFactor",
                                 cfg.get("spark.kubernetes.memoryOverheadFactor",
                                         0.1)))
    # Billed worker memory is heap+overhead rounded UP to the whole GB
    # (verified against billedResourceUtilization: 27G heap -> 30 GB billed)
    import math
    exec_mem = math.ceil(
        _parse_mem_gb(cfg.get("spark.executor.memory", "14g")) * overhead)
    drv_mem = math.ceil(
        _parse_mem_gb(cfg.get("spark.driver.memory", "14g")) * overhead)

    # Prefer lifetime-aware executor hours (extractor sums per-executor
    # add→remove uptime from the event log) over executors × app duration —
    # with dynamic allocation those differ a lot
    es = result.get("_executor_summary") or {}
    exec_core_hours = es.get("total_available_core_hours")
    exec_uptime_hours = es.get("total_uptime_hours")
    if exec_core_hours:
        vcpu = exec_core_hours + drv_cores * dur
        mem = (exec_uptime_hours or exec_core_hours / max(exec_cores, 1)) \
            * exec_mem + drv_mem * dur
        basis = "estimated (per-executor lifetimes)"
    else:
        vcpu = (execs * exec_cores + drv_cores) * dur
        mem = (execs * exec_mem + drv_mem) * dur
        basis = "estimated (executors x duration)"

    # Storage bills only beyond the free 20GB per worker
    disk_gb = _parse_mem_gb(cfg.get("spark.emr-serverless.executor.disk", "20g"))
    billable_disk = max(0.0, disk_gb - 20.0)
    sto = billable_disk * (exec_uptime_hours or execs * dur)
    if is_ec2_source:
        # EC2 runs bill EC2 instance-hours, not EMR-S units. This figure is
        # "what these resource-hours WOULD cost on Serverless" — useful for
        # migration comparisons, labeled so nobody mistakes it for the bill.
        basis = "hypothetical Serverless cost (EC2 source)"
    return {"source": basis, "vcpu_hours": round(vcpu, 3),
            "memory_gb_hours": round(mem, 3),
            "storage_gb_hours": round(sto, 3),
            "cost_usd": _emrs_cost_usd(vcpu, mem, sto)}

# AWS managed Spark Troubleshooting Agent (SigV4 MCP endpoint). Set to e.g.
# https://sagemaker-unified-studio-mcp.us-east-1.api.aws/spark-troubleshooting/mcp
# after deploying the spark-troubleshooting-mcp-setup CloudFormation stack.
SPARK_TROUBLESHOOTING_MCP_URL = os.environ.get("SPARK_TROUBLESHOOTING_MCP_URL", "")


def _advisor_home() -> Path:
    """Directory holding python_extractor.py / emr_s_fine_tuner.py."""
    return Path(os.environ.get("CONFIG_ADVISOR_HOME") or BASE_DIR)

# EMR Serverless client for the Application -> Job run selector (read-only:
# list_applications / list_job_runs). Lazy so the UI still works without
# AWS credentials — the dashboard then falls back to raw Prometheus app_ids.
_emr_client = None


def _emr():
    global _emr_client
    if _emr_client is None:
        import boto3
        from botocore.config import Config
        _emr_client = boto3.client(
            "emr-serverless", region_name=AWS_REGION,
            config=Config(retries={"mode": "adaptive", "max_attempts": 8}),
        )
    return _emr_client


# EMR Serverless job run ids look like 00abc123def456gh; anything else in the
# app_id label is a synthetic test series.
_JOB_RUN_ID_RE = re.compile(r"^[0-9a-z]{16}$")

# ListJobRuns throttles at very low TPS — cache the app->jobs resolution.
_APPS_CACHE = {}  # hours -> (expires_epoch, payload)
_APPS_CACHE_TTL = 120

# --- AI chat assistant (Bedrock) ---
MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6")
_bedrock_client = None


def _bedrock():
    global _bedrock_client
    if _bedrock_client is None:
        import boto3
        _bedrock_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock_client


CHAT_SYSTEM_PROMPT = """You are the EMR Serverless Advisor assistant, an
agentic helper embedded in a web UI with two tools: Config Advisor (event-log
analysis producing bottleneck classification and worker sizing
recommendations) and Observability (live Prometheus metrics for
driver/executors: heap, RSS, CPU, GC, disk, tasks).

PRODUCT CAPABILITIES — never claim the UI lacks these; direct users to them:
- Config Advisor page accepts UPLOADS of raw Spark event logs from BOTH
  EMR Serverless AND EMR on EC2 (YARN application_* logs): single files
  (plain/.gz/.zstd/rolling events_N), or a .zip of the log directory. It
  also accepts pre-extracted task_stage_summary JSONs. Extraction happens
  server-side; no Spark needed.
- EC2 logs are auto-detected (spark.emr_cluster_id) and routed through the
  EC2→Serverless migration branch: it translates instance-based sizing to
  Serverless workers, applies a Serverless efficiency gain to fleet sizing,
  and always maxes worker memory tier (EC2 peak metrics undercount
  Serverless needs). The result IS the migration recommendation.
- 'Compare two runs' on the same page diffs two event logs side by side:
  metric deltas, config diff, per-stage CPU-per-GB analysis.
- Home page lists applications/job runs with Optimize / Troubleshoot /
  Upgrade actions; Observability shows live metrics for metrics-enabled runs.

GROUNDING DISCIPLINE: always state WHICH run/application id an answer is
based on. If the user references a run or file that is NOT the one in your
context (e.g. an EC2 application_* id while your context holds a Serverless
run), say so explicitly and either use get_recent_analyses to find it or
tell them to upload it on the Config Advisor page — do not answer about one
run while appearing to describe another.

HARD GUARDRAILS — violating these is worse than a useless answer:
1. NEVER build a "recommended vs actually-run" config table yourself. Each
   analysis carries config_audit, computed deterministically in code with
   statuses match/differs/absent/unknown-not-in-event-log — quote it
   verbatim. If config_audit is missing, say the comparison isn't available.
2. EVENT-LOG BLIND SPOTS: submit-only params (spark.emr-serverless.executor.
   disk / disk.type, memoryOverheadFactor) and dynamicAllocation min/initial
   values NEVER reliably appear in event-log Spark conf. A value absent from
   the event log is NOT evidence it was unset — say "not visible in event
   logs; check the submitted job parameters" and ask the user for their
   submit config when it matters.
3. NUMBERS BEFORE NARRATIVE: call the relevant tool (or quote the exact
   context field) BEFORE stating any metric. Never state a score, count, or
   config value you have not read in this conversation. If you catch
   yourself writing a table with values you didn't fetch, stop and fetch.
4. Predicted improvements ("spill drops 80-90%") are ESTIMATES — label them
   as such, with the assumption they rest on. Never present a prediction in
   the same voice as a measured number.
5. When two analyses disagree (e.g. EC2-migration recommendation vs the
   Serverless-native one), present both with their sources and dates and say
   which one you'd act on and why — do not silently merge them.

You have tools. USE THEM before answering — do not guess:
- get_job_config: the job run's actual submitted Spark confs, worker sizes,
  and state from the EMR Serverless API
- get_metrics_snapshot: per-worker heap/RSS/CPU/GC/disk/task summary from
  Prometheus for a job run
- query_prometheus: any PromQL range query for drill-down (metric names use
  labels app_id=<job_run_id>, exec_id=driver|1|2|…)
- get_recent_analyses: Config Advisor results analyzed this session
  (bottleneck scores, recommended cost/perf configs)
- run_config_advisor: full pipeline on a job run's event log — bottleneck
  classification + cost/perf recommendations (slow, 30-120s; use for
  "optimize this job")
- troubleshoot_job: failure evidence — state details, driver stderr errors,
  and the AWS managed Spark Troubleshooting Agent analysis when configured
- get_application_details: release label vs latest EMR, capacity, network
- tshirt_sizing: for BRAND-NEW jobs with no run history — buckets the
  workload into our t-shirt sizes (XS/S/M/L/XL × General/Optimized/
  IO-Optimized/Iceberg-Maintenance) and returns worker type + full configs.
  Ask the user what they know (input GB, joins, shuffle, SLA, file count)
  and pass everything they give you; defaults cover the rest. For jobs that
  HAVE run, prefer run_config_advisor on the event log instead.

For "upgrade" requests: compare release_label to latest_release, then walk
through what changes between those EMR releases (Spark/Java/Scala major
versions, default conf changes, deprecations) and give a migration checklist
including validation steps. Note EMR 7.x → 8.x moves to Spark 4.x: check
ANSI SQL mode becoming default, removed Spark 2-era APIs, and Java 17+.
For the code-migration step itself, recommend the AWS Apache Spark Upgrade
Agent (managed SageMaker Unified Studio MCP server, used from an MCP client
such as Kiro CLI or Claude Code against the job's source repo): it plans the
upgrade, transforms PySpark/Scala code, fixes builds, and submits validation
jobs to EMR — see
https://docs.aws.amazon.com/emr/latest/ReleaseGuide/spark-upgrades.html.
Your checklist should cover what to verify before and after running it.

Cross-reference config and metrics: e.g. compare submitted
spark.executor.memory against observed RSS/heap max, or recommended configs
against what the job actually ran with. Quote actual numbers. Be concise and
actionable: name the config or worker change, then justify it.

Key domain facts:
- Container RSS (ProcessTree JVM+Python+Other) near the worker memory limit
  means the cgroup OOM-killer (exit 137) is close; heap-only pressure shows
  as GC ms/s climbing and heap plateauing near heap max.
- spark.driver.maxResultSize aborts large collects; driver heap holds
  collected results, so parallel collects need driver heap sized for the sum.
- EMR Serverless workers: 4 vCPU/16-30GB, 8 vCPU/32-60GB, 16 vCPU/64-120GB;
  dynamic allocation minimum is 3 executors.
- Memory spill + disk spill together = severe executor memory pressure;
  fix with fewer cores per executor, more memory per core, or more partitions.
- EMR Serverless bills per-second on vCPU-hours, memory GB-hours, and storage
  GB-hours (beyond the free 20GB/worker). us-east-1 x86: ~$0.052624/vCPU-h,
  ~$0.0057785/GB-h, ~$0.000111/storage-GB-h. get_job_config returns the run's
  billed totalResourceUtilization — use it to quantify savings in dollars
  when recommending sizing changes. run_cost marked "estimated" comes from
  event-log executor lifetimes and runs 10-25% LOW (billing includes worker
  provisioning time invisible to the event log) — say so when citing it;
  values marked "billed" are exact and match the AWS console.
Run-comparison analysis playbook (page_context.stage_comparison carries
matched per-stage CPU/GB when comparing two runs — use it):
1. Localize before theorizing: find the stage(s) whose CPU-per-GB inflated
   on identical data. If one stage owns the delta, the diagnosis is about
   that stage's operation (write/sort/join), not the whole job.
2. Same-stage CPU/GB inflation with unchanged data means the same work
   burned more cycles — the causes are worker-level: concurrent tasks per
   JVM sharing memory bandwidth/cache (stalled cycles bill as CPU time),
   GC pressure (large heap at high occupancy runs concurrent marking that
   task GC-time metrics do NOT capture), or codec/write-path changes.
   Check config_diff for executor.cores and executor.memory changes.
3. Distribution check: if p50 task time is flat but p90/p99 doubled, only
   heavy tasks suffer — classic shared-resource contention. Uniform
   slowdown suggests codec/serialization/environment change instead.
4. Heap occupancy is NOT memory demand: JVM heap fills toward whatever
   ceiling it has (GC defers collection on big heaps). Judge memory need
   from the smaller-heap run's peak + spill, normalized per core.
5. Spill is often cheap: sequential spill I/O shows as wait, not CPU.
   Weigh measured spill cost (fetch-wait %, "other" time) against the
   price of the memory that would eliminate it before recommending
   bigger workers. Fix spill with memory-per-core, not worker class.
6. Sort-heavy write stages (overwritePartitions, sortWithinPartitions)
   prefer scale-OUT (more small workers, cache-friendly working sets);
   scale-UP pays only when coordination/fetch-wait dominates.
Quantify each claim from the context data and name the config change.

MIGRATION mode (page = ec2-serverless-migration; Run A = EC2 baseline,
Run B = Serverless attempt): the user is troubleshooting an unacceptable
Serverless result. Method: (1) normalize before comparing — EC2 task-hours
include YARN scheduling/spot-loss waste that Serverless avoids; compare
per-stage CPU-per-GB, not wall-clock alone. (2) Check the Serverless config
against the migration translation (worker memory should be at tier max,
disk sized for shuffle). (3) Common migration regressions: undersized
executor disk (EC2 had big instance stores), missing spark.network.timeout
increases, maxExecutors too low vs the EC2 fleet's peak cores, and
memory-per-core changes altering sort/shuffle behavior. (4) EC2 cost in
context is hypothetical-Serverless — for a true dollar comparison ask the
user for their EC2 instance-hours bill.

QUERY PLANS: each analysis carries query_plans — per-SQL-execution physical
operator trees (like the Spark UI SQL tab) with metrics resolved from
accumulators (rows, bytes, spill, peak memory, shuffle write) and, when
tasks failed, per-operator "failures" entries: which stage failed on that
operator, task counts, reasons (FetchFailed / ExecutorLostFailure /
Resubmitted), and a sample error message. For "which operator failed" or
"where in the query" questions, walk query_plans and cite the operator
chain (e.g. Exchange <- Sort <- Window), its stage id, the reason, and the
sample message. Distinguish origin from casualty: ExecutorLostFailure with
exit 137 marks WHERE the executor died; FetchFailed/Resubmitted on other
operators are downstream casualties of that death — name the 137 site as
the root. Operator metrics (peak memory, spill size) tell you WHY it died
there.

FAILURE questions ("why did tasks fail"): ground in the actual failure
counts now present in metrics_summary (failed_tasks, killed_tasks,
failed_stages, dead_executors, dead_executor_reasons) — a handful of failed
tasks out of thousands with automatic retries is NORMAL Spark operation and
must not be narrated as a crisis. Only build a failure story from spill/
memory metrics when failure counts are material (>1% of tasks, failed
stages, or dead executors with OOM/killed reasons). For EMR Serverless runs
in this account, troubleshoot_job gives stateDetails + driver stderr; for
uploaded EC2 logs, say what evidence the event log does and doesn't carry.

If the tools lack what you need, say what is missing and how to get it — do
not invent numbers."""


# job_run_id -> application_id, filled by the selector endpoints and used by
# the chat agent's get_job_config tool
_JOB_TO_APP = {}

# Config Advisor results analyzed this session, for the chat agent
_RECENT_ANALYSES = []
_RECENT_ANALYSES_MAX = 5


def _find_application_for_job(job_run_id: str, days: int = 7):
    """Resolve a job run id to its application id (cached, bounded search)."""
    if job_run_id in _JOB_TO_APP:
        return _JOB_TO_APP[job_run_id]
    created_after = datetime.now(timezone.utc) - timedelta(days=days)
    apps = []
    for page in _emr().get_paginator("list_applications").paginate():
        apps.extend(page["applications"])
    apps = [a for a in apps if (a.get("updatedAt") or created_after) >= created_after]
    apps.sort(key=lambda a: a.get("updatedAt") or created_after, reverse=True)
    for a in apps[:10]:
        for page in _emr().get_paginator("list_job_runs").paginate(
            applicationId=a["id"], createdAtAfter=created_after,
        ):
            for jr in page["jobRuns"]:
                _JOB_TO_APP[jr["id"]] = a["id"]
        if job_run_id in _JOB_TO_APP:
            return _JOB_TO_APP[job_run_id]
    return None


def _metrics_snapshot(job_id: str, hours: int = 6) -> dict:
    """Compact per-worker metrics summary for one job run, for AI grounding."""
    end = int(time.time())
    start = end - hours * 3600
    GB = 1024 ** 3

    def qr(query):
        params = urllib.parse.urlencode(
            {"query": query, "start": start, "end": end, "step": "60s"})
        url = f"{PROMETHEUS_URL}/api/v1/query_range?{params}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.load(resp)["data"]["result"]

    sel = f'app_id="{job_id}"'
    snapshot = {"job_run_id": job_id, "window_hours": hours}

    def series_stats(query, scale=1.0):
        out = {}
        for s in qr(query):
            vals = [float(v) * scale for _, v in s["values"]]
            exec_id = s["metric"].get("exec_id", "?")
            out[exec_id] = {"last": round(vals[-1], 2), "max": round(max(vals), 2)}
        return out

    snapshot["heap_used_gb"] = series_stats(f'spark_jvm_heap_used{{{sel}}}', 1 / GB)
    snapshot["heap_max_gb"] = series_stats(f'spark_jvm_heap_max{{{sel}}}', 1 / GB)
    snapshot["rss_gb"] = series_stats(
        f'sum by (exec_id) ({{__name__=~"spark_executor_metrics_ProcessTree(JVM|Python|Other)RSSMemory", {sel}}})',
        1 / GB)
    snapshot["cpu_cores"] = series_stats(f'rate(spark_jvm_cpu_time_ns{{{sel}}}[2m]) / 1e9')
    snapshot["gc_ms_per_s"] = series_stats(
        f'sum by (exec_id) (rate(spark_executor_metrics_TotalGCTime{{{sel}}}[2m]))')
    snapshot["disk_used_gb"] = series_stats(
        f'spark_blockmanager_disk_diskSpaceUsed_MB{{{sel}}} / 1024')
    snapshot["active_tasks"] = series_stats(
        f'spark_executor_threadpool_activeTasks{{{sel}}}')
    for name, q in [
        ("jobs_succeeded", f'spark_app_jobs_succeededJobs{{{sel}}}'),
        ("jobs_failed", f'spark_app_jobs_failedJobs{{{sel}}}'),
        ("stages_completed", f'spark_app_stages_completedStages{{{sel}}}'),
        ("stages_failed", f'spark_app_stages_failedStages{{{sel}}}'),
        ("tasks_completed", f'spark_app_tasks_completedTasks{{{sel}}}'),
        ("tasks_failed", f'spark_app_tasks_failedTasks{{{sel}}}'),
    ]:
        st = series_stats(q)
        snapshot[name] = st.get("driver", {}).get("max", 0) if st else 0
    return snapshot


# ---- chat agent tools ----

def _tool_tshirt_sizing(**kwargs) -> dict:
    """Bucket a BRAND-NEW job (no event log yet) into our t-shirt sizes.

    Wraps emr_s_tshirt_size.select_bucket: Size (XS|S|M|L|XL) × sub-category
    (General | Optimized | IO-Optimized | Iceberg-Maintenance).
    """
    if str(_advisor_home()) not in sys.path:
        sys.path.insert(0, str(_advisor_home()))
    from emr_s_tshirt_size import WorkloadIntent, select_bucket

    valid = {f.name for f in WorkloadIntent.__dataclass_fields__.values()}
    params = {k: v for k, v in kwargs.items() if k in valid and v is not None}
    # select_bucket detects heavy shuffle via shuffle_ratio_pct — derive it
    # when the caller gave absolute volumes
    if ("shuffle_ratio_pct" not in params and params.get("shuffle_write_gb")
            and params.get("input_size_gb")):
        params["shuffle_ratio_pct"] = round(
            100.0 * params["shuffle_write_gb"] / params["input_size_gb"], 1)
    intent = WorkloadIntent(**params)
    b = select_bucket(intent)
    return {
        "bucket": b.label,
        "size": b.size,
        "sub_category": b.sub_bucket,
        "worker_type": b.worker_type,
        "rationale": b.rationale,
        "spark_configs": b.configs,
        "spark_submit_params": " ".join(
            f"--conf {k}={v}" for k, v in sorted(b.configs.items())),
    }


def _tool_get_application_details(application_id: str) -> dict:
    """Application config incl. release label, compared to the latest EMR."""
    a = _emr().get_application(applicationId=application_id)["application"]
    release = a.get("releaseLabel", "")
    return {
        "application_id": application_id,
        "name": a.get("name", ""),
        "state": a.get("state", ""),
        "release_label": release,
        "latest_release": EMR_LATEST_RELEASE,
        "upgrade_available": release != EMR_LATEST_RELEASE,
        "architecture": a.get("architecture", ""),
        "maximum_capacity": {k: str(v) for k, v in
                             (a.get("maximumCapacity") or {}).items()},
        "auto_stop": a.get("autoStopConfiguration", {}).get("enabled"),
        "network_configuration": bool(a.get("networkConfiguration")),
        "created_at": a["createdAt"].isoformat(),
    }


def _tool_run_config_advisor(job_run_id: str) -> dict:
    """Full Config Advisor pipeline on a finished job run: locate the event
    log from the job's S3 monitoring config, extract, and recommend."""
    cfg = _tool_get_job_config(job_run_id)
    if "error" in cfg:
        return cfg
    app_id = cfg["application_id"]
    jr = _emr().get_job_run(applicationId=app_id, jobRunId=job_run_id)["jobRun"]
    log_uri = (jr.get("configurationOverrides", {})
                 .get("monitoringConfiguration", {})
                 .get("s3MonitoringConfiguration", {})
                 .get("logUri", ""))
    if not log_uri:
        return {"error": "job has no S3 monitoring logUri — cannot locate the "
                         "Spark event log. Enable S3 monitoring on the job."}
    sparklogs = (f"{log_uri.rstrip('/')}/applications/{app_id}/jobs/"
                 f"{job_run_id}/sparklogs/")

    work = Path(tempfile.mkdtemp(prefix=f"advisor_{job_run_id}_"))
    local_logs = work / "eventlog"
    local_logs.mkdir()
    r = subprocess.run(
        ["aws", "s3", "sync", sparklogs, str(local_logs), "--region", AWS_REGION],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not any(local_logs.iterdir()):
        return {"error": f"event log download failed from {sparklogs}: "
                         f"{r.stderr.strip()[:400]}"}

    # Rolling event logs land as eventlog_v2_<appId>/ — --single-app wants
    # that inner directory, not its parent
    inner = [d for d in local_logs.iterdir() if d.is_dir()
             and d.name.startswith("eventlog_v2")]
    extract_input = inner[0] if inner else local_logs

    extracted = work / "extracted"
    r = subprocess.run(
        [sys.executable, str(_advisor_home() / "python_extractor.py"),
         "--input", str(extract_input), "--output", str(extracted),
         "--single-app"],
        capture_output=True, text=True, timeout=600, cwd=str(_advisor_home()))
    if r.returncode != 0:
        return {"error": f"extractor failed: {r.stderr.strip()[:400]}"}

    tss = extracted / "task_stage_summary"
    files = list(tss.glob("*.json")) if tss.is_dir() else []
    if not files:
        return {"error": "extractor produced no task_stage_summary JSON"}
    result = analyze_uploaded_file(str(files[0]))
    # Trim for the model: drop the verbose source config
    result.pop("spark_config_source", None)
    return result


def _tool_troubleshoot_job(job_run_id: str) -> dict:
    """Failure evidence for a job run: state details + driver stderr tail.

    If SPARK_TROUBLESHOOTING_MCP_URL is configured, also calls the AWS
    managed Spark Troubleshooting Agent (analyze_spark_workload) and
    includes its analysis verbatim.
    """
    cfg = _tool_get_job_config(job_run_id)
    if "error" in cfg:
        return cfg
    app_id = cfg["application_id"]
    jr = _emr().get_job_run(applicationId=app_id, jobRunId=job_run_id)["jobRun"]
    out = {
        "job_run_id": job_run_id,
        "application_id": app_id,
        "state": jr.get("state", ""),
        "state_details": jr.get("stateDetails", ""),
        "spark_confs": cfg.get("spark_confs", {}),
    }

    # Driver stderr tail from the S3 monitoring location
    log_uri = (jr.get("configurationOverrides", {})
                 .get("monitoringConfiguration", {})
                 .get("s3MonitoringConfiguration", {})
                 .get("logUri", ""))
    if log_uri:
        stderr_key = (f"{log_uri.rstrip('/')}/applications/{app_id}/jobs/"
                      f"{job_run_id}/SPARK_DRIVER/stderr.gz")
        try:
            import boto3
            s3 = boto3.client("s3", region_name=AWS_REGION)
            m = re.match(r"s3://([^/]+)/(.+)", stderr_key)
            obj = s3.get_object(Bucket=m.group(1), Key=m.group(2))
            text = gzip.decompress(obj["Body"].read()).decode("utf-8", "replace")
            # Errors cluster at the end; keep exception lines + the tail
            lines = text.splitlines()
            err_lines = [ln for ln in lines
                         if re.search(r"ERROR|Exception|OutOfMemory|Killed|137",
                                      ln)][-40:]
            out["driver_stderr_errors"] = err_lines
            out["driver_stderr_tail"] = lines[-60:]
        except Exception as e:
            out["driver_stderr_error"] = f"could not fetch driver stderr: {e}"
    else:
        out["driver_stderr_error"] = "job has no S3 monitoring logUri"

    # Optional: AWS managed Spark Troubleshooting Agent (SigV4 MCP endpoint)
    if SPARK_TROUBLESHOOTING_MCP_URL:
        try:
            out["aws_troubleshooting_agent"] = _call_aws_troubleshooting_mcp(
                app_id, job_run_id)
        except Exception as e:
            out["aws_troubleshooting_agent_error"] = str(e)
    else:
        out["aws_troubleshooting_agent"] = (
            "not configured — set SPARK_TROUBLESHOOTING_MCP_URL (deploy the "
            "spark-troubleshooting-mcp-setup CloudFormation stack) to include "
            "the AWS managed agent's analysis")
    return out


def _call_aws_troubleshooting_mcp(application_id: str, job_run_id: str) -> dict:
    """Call analyze_spark_workload on the AWS managed MCP server (SigV4)."""
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "analyze_spark_workload",
                   "arguments": {"platform": "EMR_SERVERLESS",
                                 "applicationId": application_id,
                                 "jobRunId": job_run_id}},
    }).encode()
    req = AWSRequest(method="POST", url=SPARK_TROUBLESHOOTING_MCP_URL,
                     data=payload,
                     headers={"Content-Type": "application/json",
                              "Accept": "application/json, text/event-stream"})
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    SigV4Auth(creds, "sagemaker-unified-studio-mcp", AWS_REGION).add_auth(req)
    http_req = urllib.request.Request(
        SPARK_TROUBLESHOOTING_MCP_URL, data=payload,
        headers=dict(req.headers), method="POST")
    with urllib.request.urlopen(http_req, timeout=180) as resp:
        return json.load(resp)


def _tool_get_job_config(job_run_id: str) -> dict:
    """Submitted Spark confs + state for a job run (EMR Serverless API)."""
    app_id = _find_application_for_job(job_run_id)
    if not app_id:
        return {"error": f"could not locate application for job run {job_run_id}"}
    jr = _emr().get_job_run(applicationId=app_id, jobRunId=job_run_id)["jobRun"]
    spark_submit = jr.get("jobDriver", {}).get("sparkSubmit", {})
    confs = {}
    for part in spark_submit.get("sparkSubmitParameters", "").split("--conf"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            confs[k.strip()] = v.strip()
    return {
        "job_run_id": job_run_id,
        "application_id": app_id,
        "name": jr.get("name", ""),
        "state": jr.get("state", ""),
        "state_details": jr.get("stateDetails", ""),
        "created_at": jr["createdAt"].isoformat(),
        "entry_point": spark_submit.get("entryPoint", ""),
        "entry_point_arguments": spark_submit.get("entryPointArguments", []),
        "spark_confs": confs,
        "total_resource_utilization": {
            k: str(v) for k, v in (jr.get("totalResourceUtilization") or {}).items()
        },
    }


def _tool_query_prometheus(query: str, hours: int = 6) -> dict:
    """Arbitrary PromQL range query, compacted for the model."""
    end = int(time.time())
    start = end - min(hours, 168) * 3600
    params = urllib.parse.urlencode(
        {"query": query, "start": start, "end": end, "step": "60s"})
    url = f"{PROMETHEUS_URL}/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        results = json.load(resp)["data"]["result"]
    out = []
    for s in results[:20]:
        vals = [float(v) for _, v in s["values"]]
        out.append({
            "labels": {k: v for k, v in s["metric"].items()
                       if k not in ("instance", "job")},
            "min": min(vals), "max": max(vals), "last": vals[-1],
            "points": len(vals),
        })
    return {"series_count": len(results), "series": out}


CHAT_TOOLS = [
    {"toolSpec": {
        "name": "get_job_config",
        "description": "Fetch the actual submitted Spark confs, entry point, worker sizing, state, and billed resource usage for an EMR Serverless job run.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"job_run_id": {"type": "string", "description": "EMR Serverless job run id, e.g. 00abc123def456gh"}},
            "required": ["job_run_id"],
        }},
    }},
    {"toolSpec": {
        "name": "get_metrics_snapshot",
        "description": "Per-worker summary (heap used/max GB, container RSS GB, CPU cores, GC ms/s, disk GB, active tasks, job/stage/task counts) from Prometheus for one job run.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "job_run_id": {"type": "string"},
                "hours": {"type": "integer", "description": "lookback window, default 6"},
            },
            "required": ["job_run_id"],
        }},
    }},
    {"toolSpec": {
        "name": "query_prometheus",
        "description": "Run any PromQL range query for drill-down (60s step). Metrics: spark_jvm_heap_used/max, spark_executor_metrics_ProcessTree{JVM,Python,Other}RSSMemory, spark_jvm_cpu_time_ns, spark_executor_metrics_TotalGCTime, spark_blockmanager_disk_diskSpaceUsed_MB, spark_executor_threadpool_activeTasks, spark_app_*, spark_dagscheduler_*. Labels: app_id=<job_run_id>, exec_id=driver|1|2|…",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "hours": {"type": "integer", "description": "lookback window, default 6"},
            },
            "required": ["query"],
        }},
    }},
    {"toolSpec": {
        "name": "get_recent_analyses",
        "description": "Config Advisor analyses run in this UI session: bottleneck classification/scores, metrics summary, source Spark config, and recommended cost/performance configs.",
        "inputSchema": {"json": {"type": "object", "properties": {}}},
    }},
    {"toolSpec": {
        "name": "run_config_advisor",
        "description": "Run the FULL Config Advisor pipeline on a finished job run: downloads its Spark event log from S3, extracts metrics, and returns bottleneck classification plus cost- and performance-optimized worker/config recommendations. Takes 30-120s. Use for 'optimize this job' requests.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"job_run_id": {"type": "string"}},
            "required": ["job_run_id"],
        }},
    }},
    {"toolSpec": {
        "name": "troubleshoot_job",
        "description": "Gather failure evidence for a job run: state details, submitted confs, driver stderr error lines and tail from S3, and (when configured) the AWS managed Spark Troubleshooting Agent's analysis. Use for failed or cancelled runs.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"job_run_id": {"type": "string"}},
            "required": ["job_run_id"],
        }},
    }},
    {"toolSpec": {
        "name": "get_application_details",
        "description": "EMR Serverless application details: release label (vs latest EMR release), capacity limits, architecture, network config. Use for upgrade questions.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"application_id": {"type": "string"}},
            "required": ["application_id"],
        }},
    }},
    {"toolSpec": {
        "name": "tshirt_sizing",
        "description": "Size a BRAND-NEW job with no run history into our t-shirt buckets (XS/S/M/L/XL x General/Optimized/IO-Optimized/Iceberg-Maintenance). Returns worker type, full Spark configs, spark-submit params, and rationale. Gather what the user knows about the workload first; every parameter is optional with sensible defaults.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "input_size_gb": {"type": "number", "description": "total input data size in GB (default 100)"},
                "workload_type": {"type": "string", "description": "etl | sql_analytics | ml | streaming | iceberg_maintenance (default etl)"},
                "num_joins": {"type": "integer", "description": "approximate number of joins (default 5)"},
                "largest_table_gb": {"type": "number", "description": "largest joined table in GB (default 50)"},
                "is_compaction": {"type": "boolean", "description": "true for Iceberg compaction/maintenance jobs"},
                "target_duration_minutes": {"type": "integer", "description": "desired runtime SLA, if any"},
                "shuffle_write_gb": {"type": "number", "description": "expected shuffle volume in GB, if known"},
                "num_files": {"type": "integer", "description": "input file count, if known (small-files detection)"},
            },
        }},
    }},
]


def _run_chat_tool(name: str, tool_input: dict):
    if name == "get_job_config":
        return _tool_get_job_config(tool_input["job_run_id"])
    if name == "get_metrics_snapshot":
        return _metrics_snapshot(tool_input["job_run_id"],
                                 int(tool_input.get("hours", 6)))
    if name == "query_prometheus":
        return _tool_query_prometheus(tool_input["query"],
                                      int(tool_input.get("hours", 6)))
    if name == "get_recent_analyses":
        return {"analyses": _RECENT_ANALYSES or
                "none this session — ask the user to run one in Config Advisor"}
    if name == "run_config_advisor":
        return _tool_run_config_advisor(tool_input["job_run_id"])
    if name == "troubleshoot_job":
        return _tool_troubleshoot_job(tool_input["job_run_id"])
    if name == "get_application_details":
        return _tool_get_application_details(tool_input["application_id"])
    if name == "tshirt_sizing":
        return _tool_tshirt_sizing(**tool_input)
    return {"error": f"unknown tool {name}"}


def _prometheus_job_ids(hours: int) -> set:
    """Job run IDs that actually have metrics in Prometheus.

    Spark's GraphiteSink prefix (the Spark app id) is the EMR Serverless
    JOB RUN id, which the exporter maps to the app_id label.
    """
    params = {
        "query": f'count by (app_id) (last_over_time(spark_jvm_heap_used[{hours}h]))',
    }
    url = f"{PROMETHEUS_URL}/api/v1/query?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.load(resp)
    return {r["metric"].get("app_id", "")
            for r in data.get("data", {}).get("result", [])}


def classify_bottleneck(metrics: dict) -> dict:
    """Classify the primary bottleneck from extracted metrics.

    Returns a dict with:
      - primary: the dominant bottleneck type
      - scores: dict of all bottleneck scores (0-100)
      - explanation: human-readable explanation
    """
    io_summary = metrics.get("io_summary", {}).get("application_level", {})
    executor_summary = metrics.get("executor_summary", {})
    spill_summary = metrics.get("spill_summary", {})
    stage_summary = metrics.get("stage_summary", {})
    stages = stage_summary.get("stages", [])

    total_input_gb = io_summary.get("total_input_gb", 0) or 0
    total_output_gb = io_summary.get("total_output_gb", 0) or 0
    total_shuffle_read_gb = io_summary.get("total_shuffle_read_gb", 0) or 0
    total_shuffle_write_gb = io_summary.get("total_shuffle_write_gb", 0) or 0
    total_shuffle_gb = total_shuffle_read_gb + total_shuffle_write_gb

    avg_cpu_pct = executor_summary.get("avg_cpu_utilization_percent", 0) or 0
    avg_mem_pct = executor_summary.get("avg_memory_utilization_percent", 0) or 0
    idle_pct = executor_summary.get("idle_core_percentage", 0) or 0

    total_mem_spill_gb = spill_summary.get("total_memory_spilled_gb", 0) or 0
    total_disk_spill_gb = spill_summary.get("total_disk_spilled_gb", 0) or 0

    total_tasks = metrics.get("task_summary", {}).get("total_tasks", 0) or 0
    duration_hours = metrics.get("total_run_duration_hours") or metrics.get("application_info", {}).get("total_run_duration_hours", 0) or 0

    # Shuffle fetch wait percent (from io_summary if available)
    shuffle_fetch_wait_pct = io_summary.get("shuffle_fetch_wait_percent", 0) or 0

    # Compute per-task averages
    total_task_exec_hours = executor_summary.get("total_task_execution_hours", 0) or 0
    avg_task_time_sec = (total_task_exec_hours * 3600 / total_tasks) if total_tasks > 0 else 0

    # --- Scoring ---
    scores = {}

    # IOPS bound: many small shuffle reads/writes, high task count relative to data
    # Indicator: lots of shuffle operations with small per-task data
    shuffle_per_task_mb = (total_shuffle_gb * 1024 / total_tasks) if total_tasks > 0 else 0
    tasks_per_gb = total_tasks / max(total_input_gb + total_shuffle_read_gb, 1)
    iops_score = 0
    if shuffle_per_task_mb > 0 and shuffle_per_task_mb < 50:
        iops_score += 40  # Small per-task shuffle = IOPS pressure
    if tasks_per_gb > 100:
        iops_score += 30  # Many tasks per GB = small random I/O
    if total_shuffle_gb > 1000 and shuffle_per_task_mb < 100:
        iops_score += 30  # Large total shuffle with small per-task = IOPS storm
    scores["IOPS Bound"] = min(100, iops_score)

    # Disk Throughput bound: large sequential shuffle writes, spill to disk
    disk_score = 0
    if total_disk_spill_gb > 100:
        disk_score += 40
    if total_shuffle_write_gb > 500:
        disk_score += 30
    if total_disk_spill_gb > total_mem_spill_gb and total_disk_spill_gb > 50:
        disk_score += 30  # Disk spill dominates memory spill
    scores["Disk Throughput Bound"] = min(100, disk_score)

    # Memory Constrained: high memory utilization, spill, GC pressure
    mem_score = 0
    if avg_mem_pct > 85:
        mem_score += 40
    elif avg_mem_pct > 70:
        mem_score += 20
    if total_mem_spill_gb > 50:
        mem_score += 30
    if total_mem_spill_gb > 0 and total_disk_spill_gb > 0:
        mem_score += 30  # Both spill types = severe memory pressure
    scores["Memory Constrained"] = min(100, mem_score)

    # CPU Constrained: high CPU utilization, low I/O wait, minimal spill
    cpu_score = 0
    if avg_cpu_pct > 80:
        cpu_score += 50
    elif avg_cpu_pct > 60:
        cpu_score += 30
    if total_mem_spill_gb < 10 and total_disk_spill_gb < 10:
        cpu_score += 25  # Low spill = not memory-bound
    if idle_pct < 20:
        cpu_score += 25  # Cores fully utilized
    scores["CPU Constrained"] = min(100, cpu_score)

    # Network Bound: high shuffle, fetch wait dominates, high shuffle ratio
    net_score = 0
    if shuffle_fetch_wait_pct > 30:
        net_score += 50
    elif shuffle_fetch_wait_pct > 15:
        net_score += 25
    shuffle_ratio = total_shuffle_gb / max(total_input_gb, 1)
    if shuffle_ratio > 5:
        net_score += 30
    elif shuffle_ratio > 2:
        net_score += 15
    if total_shuffle_gb > 2000:
        net_score += 20
    scores["Network Bound"] = min(100, net_score)

    # Determine primary bottleneck
    primary = max(scores, key=scores.get) if any(v > 0 for v in scores.values()) else "Balanced"
    primary_score = scores.get(primary, 0)

    # Generate explanation
    explanations = {
        "IOPS Bound": f"High task count ({total_tasks:,}) with small per-task shuffle ({shuffle_per_task_mb:.0f} MB/task) creates IOPS pressure on shuffle disks.",
        "Disk Throughput Bound": f"Large disk spill ({total_disk_spill_gb:.0f} GB) and shuffle writes ({total_shuffle_write_gb:.0f} GB) saturate disk throughput.",
        "Memory Constrained": f"High memory utilization ({avg_mem_pct:.0f}%) with {total_mem_spill_gb:.0f} GB memory spill indicates insufficient executor memory.",
        "CPU Constrained": f"High CPU utilization ({avg_cpu_pct:.0f}%) with low idle ({idle_pct:.0f}%) and minimal spill — compute-bound workload.",
        "Network Bound": f"Shuffle fetch wait at {shuffle_fetch_wait_pct:.0f}% with {total_shuffle_gb:.0f} GB total shuffle — network/shuffle serving is the bottleneck.",
        "Balanced": "No single dominant bottleneck detected — workload is relatively balanced.",
    }

    return {
        "primary": primary,
        "primary_score": primary_score,
        "scores": scores,
        "explanation": explanations.get(primary, ""),
        "avg_task_time_sec": round(avg_task_time_sec, 2),
        "total_tasks": total_tasks,
        "duration_hours": round(duration_hours, 2),
        "total_task_exec_hours": round(total_task_exec_hours, 2),
    }


def analyze_uploaded_file(json_path: str) -> dict:
    """Run the full analysis on an uploaded extracted JSON file.

    Expects a task_stage_summary JSON (output of spark_extractor.py).
    Returns recommendation + bottleneck analysis.
    """
    # Write the file into a temp directory structure the fine tuner expects
    with open(json_path) as f:
        data = json.load(f)

    app_id = data.get("application_id", "unknown")

    # Create temp dir with task_stage_summary/ subdirectory
    staging_dir = UPLOAD_DIR / f"staging_{app_id}"
    tss_dir = staging_dir / "task_stage_summary"
    tss_dir.mkdir(parents=True, exist_ok=True)

    # Write the JSON file
    output_file = tss_dir / f"{app_id}.json"
    with open(output_file, "w") as f:
        json.dump(data, f)

    # Run the fine tuner
    try:
        cost_recs, perf_recs = generate_dual_recommendations(
            str(staging_dir), limit=1, target_partition_size_mib=1024
        )
    except Exception as e:
        return {"error": f"Fine tuner failed: {str(e)}"}

    cost_rec = cost_recs[0] if cost_recs else {}
    perf_rec = perf_recs[0] if perf_recs else {}

    # Classify bottleneck from the raw metrics
    bottleneck = classify_bottleneck(data)

    # Build result
    result = {
        "application_id": app_id,
        "application_name": data.get("application_info", {}).get("application_name", "N/A"),
        "duration_hours": data.get("total_run_duration_hours") or data.get("application_info", {}).get("total_run_duration_hours", 0),
        "bottleneck": bottleneck,
        "cost_recommendation": cost_rec,
        "perf_recommendation": perf_rec,
        "metrics_summary": {
            "total_input_gb": data.get("io_summary", {}).get("application_level", {}).get("total_input_gb", 0),
            "total_shuffle_read_gb": data.get("io_summary", {}).get("application_level", {}).get("total_shuffle_read_gb", 0),
            "total_shuffle_write_gb": data.get("io_summary", {}).get("application_level", {}).get("total_shuffle_write_gb", 0),
            "total_memory_spill_gb": data.get("spill_summary", {}).get("total_memory_spilled_gb", 0),
            "total_disk_spill_gb": data.get("spill_summary", {}).get("total_disk_spilled_gb", 0),
            "avg_memory_util_pct": data.get("executor_summary", {}).get("avg_memory_utilization_percent", 0),
            "avg_cpu_util_pct": data.get("executor_summary", {}).get("avg_cpu_utilization_percent", 0),
            "idle_core_pct": data.get("executor_summary", {}).get("idle_core_percentage", 0),
            "total_executors": data.get("executor_summary", {}).get("total_executors", 0),
            "total_tasks": data.get("task_summary", {}).get("total_tasks", 0),
            "total_stages": data.get("stage_summary", {}).get("total_stages", 0),
            "failed_tasks": data.get("task_summary", {}).get("failed_tasks", 0),
            "killed_tasks": data.get("task_summary", {}).get("killed_tasks", 0),
            "failed_stages": data.get("stage_summary", {}).get("failed_stages", 0),
            "dead_executors": data.get("executor_summary", {}).get("dead_executors", 0),
            "dead_executor_reasons": data.get("executor_summary", {}).get("dead_executor_reasons", {}),
        },
        "source_platform": ("emr-ec2" if data.get("spark_config", {}).get("spark.emr_cluster_id")
                            else "emr-serverless"),
        "spark_config_source": data.get("spark_config", {}),
    }
    try:
        result["_executor_summary"] = {
            k: data.get("executor_summary", {}).get(k)
            for k in ("total_available_core_hours", "total_uptime_hours")
        }
        result["run_cost"] = _run_cost(result)
    except Exception:
        result["run_cost"] = None
    finally:
        result.pop("_executor_summary", None)

    # Per-stage records for the compare view's stage-level diff (stripped
    # before rendering single-run pages — see /analyze)
    result["_stages"] = data.get("stage_summary", {}).get("stages", [])

    # Deterministic recommended-vs-submitted comparison — the chat agent
    # must cite this instead of reconstructing config diffs itself
    try:
        result["config_audit"] = _config_audit(result)
    except Exception:
        result["config_audit"] = []

    # Query profiles (Spark-UI-style operator trees with metrics)
    result["query_plans"] = data.get("query_plans", [])

    # Cleanup staging
    import shutil
    shutil.rmtree(staging_dir, ignore_errors=True)

    # Keep for the chat agent's get_recent_analyses tool (dedupe by app)
    _RECENT_ANALYSES[:] = [a for a in _RECENT_ANALYSES
                           if a.get("application_id") != app_id]
    _RECENT_ANALYSES.append(result)
    del _RECENT_ANALYSES[:-_RECENT_ANALYSES_MAX]

    return result


# Submit-level params that NEVER appear in event-log Spark conf — claiming
# these were "not set" based on an event log is fabrication
_CONFS_INVISIBLE_IN_EVENT_LOGS = (
    "spark.emr-serverless.executor.disk",
    "spark.emr-serverless.executor.disk.type",
    "spark.emr-serverless.driver.disk",
    "spark.emr-serverless.memoryOverheadFactor",
)


def _config_audit(result: dict) -> list:
    """Deterministic recommended-vs-submitted config comparison.

    Computed in code so the chat agent cannot fabricate it. status values:
    match | differs | absent | unknown-not-in-event-log.
    """
    submitted = result.get("spark_config_source") or {}
    rec = (result.get("cost_recommendation") or {}).get("spark_configs") or {}
    rows = []
    for k in sorted(rec):
        sub = submitted.get(k)
        if sub in (None, "None", ""):
            # Submit-only params aren't reliably logged: absence is NOT
            # evidence of being unset
            if k in _CONFS_INVISIBLE_IN_EVENT_LOGS:
                rows.append({"key": k, "recommended": rec[k],
                             "submitted": "(not captured in this event log)",
                             "status": "unknown-not-in-event-log"})
                continue
            status = "absent"
        elif str(sub).strip().lower() == str(rec[k]).strip().lower():
            status = "match"
        else:
            status = "differs"
        rows.append({"key": k, "recommended": rec[k],
                     "submitted": sub, "status": status})
    return rows


def _looks_like_extracted_json(path: Path) -> bool:
    """True if the upload is already a task_stage_summary JSON (vs a raw
    Spark event log, which is JSON-lines of SparkListener events)."""
    if path.suffix != ".json":
        return False
    try:
        with open(path) as f:
            head = f.read(4096).lstrip()
        if not head.startswith("{"):
            return False
        # Event logs start with {"Event":"SparkListenerLogStart",...}
        first_line = head.splitlines()[0]
        return '"Event"' not in first_line
    except Exception:
        return False


def _extract_raw_event_log(upload_path: Path) -> Path:
    """Run python_extractor on a raw event log upload; returns the
    task_stage_summary JSON path. Raises ValueError with a user-facing
    message on failure.

    Accepts: a single event log file (plain / .gz / .lz4 / .zstd, including
    a rolling events_N_appId file) or a .zip of an eventlog_v2_<appId>/
    rolling directory.
    """
    work = Path(tempfile.mkdtemp(prefix="advisor_rawlog_"))
    # The extractor derives the application name from this directory name —
    # use the upload's stem so results show a meaningful id
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_",
                  upload_path.stem.replace(".zip", "")) or "eventlog"
    logs_dir = work / stem
    logs_dir.mkdir()

    if upload_path.suffix == ".zip":
        with zipfile.ZipFile(upload_path) as z:
            total = sum(i.file_size for i in z.infolist())
            if total > MAX_UPLOAD_BYTES * 4:
                raise ValueError("Zip expands beyond the size limit")
            z.extractall(logs_dir)
    else:
        shutil.copy(upload_path, logs_dir / upload_path.name)

    # Rolling logs may arrive as eventlog_v2_<appId>/ inside the zip —
    # the extractor's --single-app wants that inner directory
    inner = [d for d in logs_dir.rglob("eventlog_v2*") if d.is_dir()]
    extract_input = inner[0] if inner else logs_dir

    extracted = work / "extracted"
    r = subprocess.run(
        [sys.executable, str(_advisor_home() / "python_extractor.py"),
         "--input", str(extract_input), "--output", str(extracted),
         "--single-app"],
        capture_output=True, text=True, timeout=900, cwd=str(_advisor_home()))
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip()[-600:]
        raise ValueError(f"Event log extraction failed: {tail}")

    tss = extracted / "task_stage_summary"
    files = list(tss.glob("*.json")) if tss.is_dir() else []
    if not files:
        raise ValueError("Extractor produced no task_stage_summary output — "
                         "is this a Spark event log?")
    return files[0]


async def _save_upload(file: UploadFile) -> Path:
    """Stream an upload to disk, enforcing the size cap."""
    upload_path = UPLOAD_DIR / Path(file.filename).name
    written = 0
    with open(upload_path, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                out.close()
                upload_path.unlink(missing_ok=True)
                raise ValueError("File exceeds 500MB upload limit")
            out.write(chunk)
    return upload_path


def _redirect_with_error(message: str) -> RedirectResponse:
    q = urllib.parse.urlencode({"error": message})
    return RedirectResponse(url=f"/config-advisor?{q}", status_code=303)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/")
def home(request: Request):
    """Landing page — navigate to Config Advisor or Observability."""
    return templates.TemplateResponse(request, "landing.html", {"active": "home"})


@app.get("/config-advisor")
def config_advisor(request: Request, error: str = ""):
    """Upload form for extracted event log JSON."""
    return templates.TemplateResponse(
        request, "config_advisor.html",
        {"active": "config-advisor", "error": error},
    )


ANALYZE_EXTS = (".json", ".zip", ".gz", ".lz4", ".zstd", ".inprogress", "")


def _prepare_analysis_input(upload_path: Path) -> Path:
    """Return a task_stage_summary JSON for the upload — extracting first
    when it's a raw Spark event log."""
    if _looks_like_extracted_json(upload_path):
        return upload_path
    return _extract_raw_event_log(upload_path)


@app.post("/analyze")
async def analyze(request: Request, file: UploadFile = File(...)):
    if not file.filename:
        return _redirect_with_error("No file selected")
    if not Path(file.filename).suffix.lower() in ANALYZE_EXTS:
        return _redirect_with_error(
            "Upload a task_stage_summary JSON, or a raw Spark event log "
            "(plain / .gz / rolling events_* file, or .zip of an "
            "eventlog_v2 directory)")

    try:
        upload_path = await _save_upload(file)
    except ValueError as e:
        return _redirect_with_error(str(e))

    try:
        result = analyze_uploaded_file(str(_prepare_analysis_input(upload_path)))
    except ValueError as e:
        return _redirect_with_error(str(e))
    except Exception as e:
        return _redirect_with_error(f"Analysis failed: {str(e)}")
    finally:
        upload_path.unlink(missing_ok=True)

    if "error" in result:
        return _redirect_with_error(result["error"])

    result.pop("_stages", None)
    return templates.TemplateResponse(
        request, "results.html",
        {"active": "config-advisor", "result": result},
    )


def _compare_metric_rows(a: dict, b: dict) -> list:
    """Build [(label, val_a, val_b, delta_pct, better)] rows for the compare
    view. 'better' marks which side wins when the metric has a direction."""
    ms_a, ms_b = a["metrics_summary"], b["metrics_summary"]
    bn_a, bn_b = a["bottleneck"], b["bottleneck"]

    def row(label, va, vb, unit="", lower_is_better=None, fmt="{:,.1f}"):
        delta = None
        if va and vb:
            delta = round((vb - va) / va * 100, 1)
        better = None
        if lower_is_better is not None and va != vb and va is not None and vb is not None:
            better = ("a" if (va < vb) == lower_is_better else "b")
        return {"label": label, "a": fmt.format(va or 0), "b": fmt.format(vb or 0),
                "unit": unit, "delta_pct": delta, "better": better}

    rows = []
    ca, cb = a.get("run_cost"), b.get("run_cost")
    if ca and cb:
        all_billed = ca["source"] == "billed" and cb["source"] == "billed"
        cost_label = "Cost (USD, billed)" if all_billed else "Cost (USD, ESTIMATED)"
        rows += [
            row(cost_label, ca["cost_usd"], cb["cost_usd"], "$",
                lower_is_better=True, fmt="{:,.4f}"),
            row("vCPU-hours", ca["vcpu_hours"], cb["vcpu_hours"], "",
                lower_is_better=True, fmt="{:,.2f}"),
            row("Memory GB-hours", ca["memory_gb_hours"], cb["memory_gb_hours"],
                "", lower_is_better=True, fmt="{:,.2f}"),
        ]
    return rows + [
        row("Duration", a.get("duration_hours") or 0, b.get("duration_hours") or 0,
            "h", lower_is_better=True, fmt="{:,.2f}"),
        row("Total tasks", ms_a["total_tasks"], ms_b["total_tasks"], fmt="{:,.0f}"),
        row("Total stages", ms_a["total_stages"], ms_b["total_stages"], fmt="{:,.0f}"),
        row("Executors", ms_a["total_executors"], ms_b["total_executors"], fmt="{:,.0f}"),
        row("Avg task time", bn_a["avg_task_time_sec"], bn_b["avg_task_time_sec"],
            "s", lower_is_better=True, fmt="{:,.2f}"),
        row("Task execution", bn_a["total_task_exec_hours"], bn_b["total_task_exec_hours"],
            "h", lower_is_better=True, fmt="{:,.2f}"),
        row("Input", ms_a["total_input_gb"], ms_b["total_input_gb"], "GB"),
        row("Shuffle read", ms_a["total_shuffle_read_gb"], ms_b["total_shuffle_read_gb"], "GB"),
        row("Shuffle write", ms_a["total_shuffle_write_gb"], ms_b["total_shuffle_write_gb"], "GB"),
        row("Memory spill", ms_a["total_memory_spill_gb"], ms_b["total_memory_spill_gb"],
            "GB", lower_is_better=True),
        row("Disk spill", ms_a["total_disk_spill_gb"], ms_b["total_disk_spill_gb"],
            "GB", lower_is_better=True),
        row("Avg CPU util", ms_a["avg_cpu_util_pct"], ms_b["avg_cpu_util_pct"], "%"),
        row("Avg memory util", ms_a["avg_memory_util_pct"], ms_b["avg_memory_util_pct"], "%"),
        row("Idle cores", ms_a["idle_core_pct"], ms_b["idle_core_pct"], "%",
            lower_is_better=True),
    ]


def _compare_stages(a: dict, b: dict, top_n: int = 8) -> dict:
    """Stage-level A/B comparison keyed on (stage name, data volume) — stage
    IDs shift between runs, but the same query plan produces the same named
    stages over the same data. Computes CPU-per-GB inflation to localize
    where extra compute burn lives (contention, GC, codec changes)."""
    def index(res):
        out = {}
        for s in (res.get("_stages") or []):
            data_gb = (s.get("input_gb", 0) + s.get("shuffle_read_gb", 0))
            # bucket data volume to 2 significant figures for matching
            key = (s.get("name", "")[:60], f"{data_gb:.2g}")
            if key not in out or s.get("total_cpu_time_sec", 0) > out[key].get("total_cpu_time_sec", 0):
                out[key] = s
        return out

    ia, ib = index(a), index(b)
    matched = []
    for key in ia.keys() & ib.keys():
        sa, sb = ia[key], ib[key]
        cpu_a = sa.get("total_cpu_time_sec", 0)
        cpu_b = sb.get("total_cpu_time_sec", 0)
        data_gb = max(sa.get("input_gb", 0) + sa.get("shuffle_read_gb", 0), 0.01)
        if cpu_a < 60 and cpu_b < 60:  # skip trivial stages
            continue
        rate_a = cpu_a / data_gb
        rate_b = cpu_b / max(sb.get("input_gb", 0) + sb.get("shuffle_read_gb", 0), 0.01)
        matched.append({
            "name": key[0],
            "stage_a": sa.get("stage_id"), "stage_b": sb.get("stage_id"),
            "tasks_a": sa.get("tasks_completed"), "tasks_b": sb.get("tasks_completed"),
            "data_gb": round(data_gb, 1),
            "cpu_hours_a": round(cpu_a / 3600, 2),
            "cpu_hours_b": round(cpu_b / 3600, 2),
            "cpu_per_gb_a": round(rate_a, 1),
            "cpu_per_gb_b": round(rate_b, 1),
            "inflation_pct": round((rate_b / rate_a - 1) * 100, 1) if rate_a else None,
            "spill_gb_a": round(sa.get("mem_spill_gb", 0) + sa.get("disk_spill_gb", 0), 1),
            "spill_gb_b": round(sb.get("mem_spill_gb", 0) + sb.get("disk_spill_gb", 0), 1),
        })
    matched.sort(key=lambda m: -(m["cpu_hours_a"] + m["cpu_hours_b"]))
    matched = matched[:top_n]

    total_a = sum(m["cpu_hours_a"] for m in matched)
    total_b = sum(m["cpu_hours_b"] for m in matched)
    culprit = max(matched, key=lambda m: abs(m["cpu_hours_b"] - m["cpu_hours_a"]),
                  default=None)
    verdict = ""
    if culprit and abs(culprit["cpu_hours_b"] - culprit["cpu_hours_a"]) > 0.5:
        delta = culprit["cpu_hours_b"] - culprit["cpu_hours_a"]
        total_delta = total_b - total_a
        share = abs(delta) / max(abs(total_delta), 0.01) * 100
        verdict = (f"Stage '{culprit['name']}' accounts for ~{min(share, 100):.0f}% of the "
                   f"CPU-hour difference ({culprit['cpu_hours_a']} → {culprit['cpu_hours_b']} h, "
                   f"CPU/GB {'+' if culprit['inflation_pct'] >= 0 else ''}{culprit['inflation_pct']}%). "
                   "Same-stage CPU-per-GB inflation on identical data usually means "
                   "worker-level contention (concurrent tasks sharing memory bandwidth), "
                   "GC pressure from a larger heap, or a codec/write-path change — "
                   "not more work.")
    return {"stages": matched, "verdict": verdict}


def _compare_config_diff(a: dict, b: dict) -> list:
    """Spark confs that differ between the two runs (noise filtered)."""
    ca = a.get("spark_config_source") or {}
    cb = b.get("spark_config_source") or {}
    noise = re.compile(
        r"(app\.id|app\.name|driver\.host|jars|eventLog\.dir|submitTime|"
        r"driver\.appUIAddress|fileserver|\.port$|app\.startTime|"
        r"emr\.job\.id|\.uuid|attemptId|proxyBase)", re.I)
    diff = []
    for k in sorted(set(ca) | set(cb)):
        va, vb = ca.get(k), cb.get(k)
        if va != vb and not noise.search(k):
            diff.append({"key": k, "a": va or "—", "b": vb or "—"})
    return diff


@app.post("/analyze-compare")
async def analyze_compare(request: Request,
                          file_a: UploadFile = File(...),
                          file_b: UploadFile = File(...)):
    """Side-by-side comparison of two event logs of the same job."""
    for f in (file_a, file_b):
        if not f.filename or Path(f.filename).suffix.lower() not in ANALYZE_EXTS:
            return _redirect_with_error(
                "Both files must be task_stage_summary JSONs or raw Spark event logs")

    results = []
    try:
        for f in (file_a, file_b):
            upload_path = await _save_upload(f)
            try:
                r = analyze_uploaded_file(str(_prepare_analysis_input(upload_path)))
                if "error" in r:
                    return _redirect_with_error(f"{f.filename}: {r['error']}")
                r["source_filename"] = f.filename
                results.append(r)
            finally:
                upload_path.unlink(missing_ok=True)
    except ValueError as e:
        return _redirect_with_error(str(e))
    except Exception as e:
        return _redirect_with_error(f"Comparison failed: {e}")

    a, b = results
    stage_cmp = _compare_stages(a, b)
    a.pop("_stages", None)
    b.pop("_stages", None)
    return templates.TemplateResponse(
        request, "compare_results.html",
        {"active": "config-advisor", "a": a, "b": b,
         "rows": _compare_metric_rows(a, b),
         "config_diff": _compare_config_diff(a, b),
         "stage_cmp": stage_cmp},
    )


@app.post("/analyze-migration")
async def analyze_migration(request: Request,
                            file_ec2: UploadFile = File(None),
                            file_sls: UploadFile = File(None)):
    """EC2 -> Serverless migration workbench.

    One file: full analysis (EC2 logs get the migration-translated configs).
    Both files: the compare view, EC2 as Run A / Serverless as Run B, with a
    migration verdict so a disappointing Serverless run can be troubleshot
    against its EC2 baseline.
    """
    uploads = [(f, tag) for f, tag in ((file_ec2, "EC2"), (file_sls, "Serverless"))
               if f is not None and f.filename]
    if not uploads:
        return _redirect_with_error("Upload at least one event log (EC2 or Serverless)")
    for f, _ in uploads:
        if Path(f.filename).suffix.lower() not in ANALYZE_EXTS:
            return _redirect_with_error(f"{f.filename}: unsupported file type")

    results = []
    try:
        for f, tag in uploads:
            upload_path = await _save_upload(f)
            try:
                r = analyze_uploaded_file(str(_prepare_analysis_input(upload_path)))
                if "error" in r:
                    return _redirect_with_error(f"{f.filename}: {r['error']}")
                r["source_filename"] = f"[{tag}] {f.filename}"
                # sanity: warn-but-proceed when the platform doesn't match the slot
                expected = "emr-ec2" if tag == "EC2" else "emr-serverless"
                r["slot_mismatch"] = (r.get("source_platform") != expected)
                results.append(r)
            finally:
                upload_path.unlink(missing_ok=True)
    except ValueError as e:
        return _redirect_with_error(str(e))
    except Exception as e:
        return _redirect_with_error(f"Migration analysis failed: {e}")

    if len(results) == 1:
        r = results[0]
        r.pop("_stages", None)
        return templates.TemplateResponse(
            request, "results.html",
            {"active": "config-advisor", "result": r})

    a, b = results  # EC2 = A, Serverless = B
    stage_cmp = _compare_stages(a, b)
    a.pop("_stages", None)
    b.pop("_stages", None)
    return templates.TemplateResponse(
        request, "compare_results.html",
        {"active": "config-advisor", "a": a, "b": b,
         "rows": _compare_metric_rows(a, b),
         "config_diff": _compare_config_diff(a, b),
         "stage_cmp": stage_cmp,
         "migration_mode": True},
    )


@app.get("/metrics-dashboard")
def metrics_dashboard(request: Request, app_id: str = ""):
    """Live/replay Spark worker metrics (heap, RSS, CPU, GC) from Prometheus."""
    return templates.TemplateResponse(
        request, "metrics_dashboard.html",
        {"active": "observability", "app_id": app_id},
    )


# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------

@app.get("/api/metrics/query_range")
def metrics_query_range(query: str = "", start: str = "", end: str = "", step: str = "15s"):
    """Proxy to the Prometheus query_range API.

    Keeps Prometheus unexposed to the browser and gives one place to swap
    the datasource (OSS Prometheus vs AMP+SigV4) later.
    """
    if not query:
        return JSONResponse({"error": "query parameter required"}, status_code=400)
    params = {"query": query, "start": start, "end": end, "step": step}
    url = f"{PROMETHEUS_URL}/api/v1/query_range?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return JSONResponse(json.load(resp))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/metrics/apps")
def metrics_apps(hours: int = 24):
    """List Spark app_ids (= EMR Serverless job run ids) present in Prometheus."""
    try:
        return JSONResponse({"apps": sorted(_prometheus_job_ids(hours))})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/emr/applications")
def emr_applications(hours: int = 48):
    """EMR Serverless applications that have metrics-bearing job runs.

    Cross-references list_applications/list_job_runs (read-only) with the
    job run ids present in Prometheus, so the selector only offers
    applications with something to show.
    """
    cached = _APPS_CACHE.get(hours)
    if cached and cached[0] > time.time():
        return JSONResponse(cached[1])

    try:
        metric_jobs = {j for j in _prometheus_job_ids(hours)
                       if _JOB_RUN_ID_RE.match(j)}
    except Exception as e:
        return JSONResponse({"error": f"prometheus: {e}"}, status_code=502)

    created_after = datetime.now(timezone.utc) - timedelta(hours=hours)
    apps = []
    unmatched = set(metric_jobs)
    try:
        all_apps = []
        for page in _emr().get_paginator("list_applications").paginate():
            all_apps.extend(page["applications"])
        # Most-recently-updated apps first, and stop as soon as every
        # metrics-bearing job run is accounted for — avoids paging
        # list_job_runs across dozens of idle apps (the API throttles at
        # very low TPS). Apps not touched within the window can't own a
        # job run created in it.
        all_apps = [a for a in all_apps
                    if (a.get("updatedAt") or created_after) >= created_after]
        all_apps.sort(key=lambda a: a.get("updatedAt") or created_after,
                      reverse=True)
        for a in all_apps:
            if not unmatched:
                break
            app_id = a["id"]
            matched = []
            for jr_page in _emr().get_paginator("list_job_runs").paginate(
                applicationId=app_id, createdAtAfter=created_after,
            ):
                matched.extend(
                    jr["id"] for jr in jr_page["jobRuns"]
                    if jr["id"] in metric_jobs
                )
            if matched:
                unmatched.difference_update(matched)
                apps.append({
                    "id": app_id,
                    "name": a.get("name", app_id),
                    "state": a.get("state", ""),
                    "metricJobCount": len(matched),
                })
        payload = {"applications": apps}
        _APPS_CACHE[hours] = (time.time() + _APPS_CACHE_TTL, payload)
        return JSONResponse(payload)
    except Exception as e:
        # No AWS creds / API failure: dashboard falls back to raw job ids
        return JSONResponse({"error": f"emr-serverless: {e}", "applications": []},
                            status_code=200)


@app.get("/api/home/overview")
def home_overview(hours: int = 72, max_apps: int = 8, max_jobs: int = 10):
    """Applications + recent job runs for the home page action table.

    Each app carries releaseLabel vs EMR_LATEST_RELEASE (drives Upgrade);
    each job run carries state (drives Troubleshoot) and whether metrics
    exist in Prometheus (annotates Observability links).
    """
    cache_key = ("overview", hours, max_apps, max_jobs)
    cached = _APPS_CACHE.get(cache_key)
    if cached and cached[0] > time.time():
        return JSONResponse(cached[1])

    try:
        metric_jobs = _prometheus_job_ids(hours)
    except Exception:
        metric_jobs = set()

    created_after = datetime.now(timezone.utc) - timedelta(hours=hours)
    out_apps = []
    try:
        all_apps = []
        for page in _emr().get_paginator("list_applications").paginate():
            all_apps.extend(page["applications"])
        all_apps = [a for a in all_apps
                    if (a.get("updatedAt") or created_after) >= created_after]
        all_apps.sort(key=lambda a: a.get("updatedAt") or created_after,
                      reverse=True)
        for a in all_apps[:max_apps]:
            app_id = a["id"]
            release = a.get("releaseLabel", "")
            jobs = []
            for page in _emr().get_paginator("list_job_runs").paginate(
                applicationId=app_id, createdAtAfter=created_after,
            ):
                for jr in page["jobRuns"]:
                    _JOB_TO_APP[jr["id"]] = app_id
                    jobs.append({
                        "id": jr["id"],
                        "name": jr.get("name", jr["id"]),
                        "state": jr.get("state", ""),
                        "createdAt": jr["createdAt"].isoformat(),
                        "hasMetrics": jr["id"] in metric_jobs,
                    })
            jobs.sort(key=lambda j: j["createdAt"], reverse=True)
            out_apps.append({
                "id": app_id,
                "name": a.get("name", app_id),
                "state": a.get("state", ""),
                "releaseLabel": release,
                "upgradeAvailable": release != EMR_LATEST_RELEASE,
                "jobs": jobs[:max_jobs],
            })
        payload = {"applications": out_apps,
                   "latestRelease": EMR_LATEST_RELEASE}
        _APPS_CACHE[cache_key] = (time.time() + _APPS_CACHE_TTL, payload)
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({"error": f"emr-serverless: {e}",
                             "applications": [],
                             "latestRelease": EMR_LATEST_RELEASE})


@app.get("/api/emr/jobs")
def emr_jobs(application_id: str, hours: int = 48):
    """Job runs of one EMR Serverless application that have metrics."""
    try:
        metric_jobs = _prometheus_job_ids(hours)
    except Exception as e:
        return JSONResponse({"error": f"prometheus: {e}"}, status_code=502)

    created_after = datetime.now(timezone.utc) - timedelta(hours=hours)
    jobs = []
    try:
        paginator = _emr().get_paginator("list_job_runs")
        for page in paginator.paginate(
            applicationId=application_id, createdAtAfter=created_after,
        ):
            for jr in page["jobRuns"]:
                _JOB_TO_APP[jr["id"]] = application_id
                if jr["id"] not in metric_jobs:
                    continue
                jobs.append({
                    "id": jr["id"],
                    "name": jr.get("name", jr["id"]),
                    "state": jr.get("state", ""),
                    "createdAt": jr["createdAt"].isoformat(),
                })
        jobs.sort(key=lambda j: j["createdAt"], reverse=True)
        return JSONResponse({"jobs": jobs})
    except Exception as e:
        return JSONResponse({"error": f"emr-serverless: {e}", "jobs": []},
                            status_code=200)


@app.post("/api/chat")
async def api_chat(request: Request):
    """Agentic chat: Bedrock Converse loop with tools over the EMR Serverless
    API, Prometheus, and this session's Config Advisor results.

    Body: {"messages": [{"role": "user"|"assistant", "content": "..."}],
           "page_context": {...}}   (page_context optional)
    Streams NDJSON events: {"type": "tool", "name", "input"} while the agent
    works, then {"type": "text", "text"} chunks, then {"type": "done"}.
    """
    body = await request.json()
    messages = [
        {"role": m["role"], "content": [{"text": m["content"]}]}
        for m in body.get("messages", [])
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    if not messages:
        return JSONResponse({"error": "messages required"}, status_code=400)

    page_context = body.get("page_context") or {}
    system = [{"text": CHAT_SYSTEM_PROMPT
               + "\n\nCurrent page context:\n" + json.dumps(page_context, default=str)}]

    def agent_loop():
        convo = list(messages)
        for _turn in range(8):  # tool-use iteration cap
            resp = _bedrock().converse(
                modelId=MODEL_ID,
                system=system,
                messages=convo,
                toolConfig={"tools": CHAT_TOOLS},
                inferenceConfig={"maxTokens": 4000},
            )
            out_msg = resp["output"]["message"]
            convo.append(out_msg)

            if resp["stopReason"] != "tool_use":
                for block in out_msg["content"]:
                    if "text" in block:
                        yield json.dumps({"type": "text", "text": block["text"]}) + "\n"
                yield json.dumps({"type": "done"}) + "\n"
                return

            tool_results = []
            for block in out_msg["content"]:
                if "toolUse" not in block:
                    continue
                tu = block["toolUse"]
                yield json.dumps({"type": "tool", "name": tu["name"],
                                  "input": tu["input"]}) + "\n"
                try:
                    result = _run_chat_tool(tu["name"], tu["input"])
                    content = [{"json": result}]
                    status = "success"
                except Exception as e:
                    content = [{"text": f"tool error: {e}"}]
                    status = "error"
                tool_results.append({"toolResult": {
                    "toolUseId": tu["toolUseId"],
                    "content": content, "status": status,
                }})
            convo.append({"role": "user", "content": tool_results})

        yield json.dumps({"type": "text",
                          "text": "(stopped: tool iteration limit reached)"}) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(agent_loop(), media_type="application/x-ndjson")


@app.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...)):
    """JSON API endpoint for programmatic access.

    Accepts a task_stage_summary JSON or a raw Spark event log
    (plain / .gz / rolling / .zip) — raw logs are extracted server-side.
    """
    if not file.filename or Path(file.filename).suffix.lower() not in ANALYZE_EXTS:
        return JSONResponse(
            {"error": "Upload a task_stage_summary JSON or a raw Spark event log"},
            status_code=400)

    try:
        upload_path = await _save_upload(file)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=413)

    try:
        result = analyze_uploaded_file(str(_prepare_analysis_input(upload_path)))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        upload_path.unlink(missing_ok=True)

    return JSONResponse(result)


if __name__ == "__main__":
    print("EMR Serverless Advisor — FastAPI UI")
    print("=" * 50)
    print("  /                   landing page")
    print("  /config-advisor     event log analysis + recommendations")
    print("  /metrics-dashboard  live worker metrics (Prometheus)")
    print("  /docs               auto-generated API docs")
    print("=" * 50)
    print("\nStarting on http://0.0.0.0:5000")
    uvicorn.run(app, host="0.0.0.0", port=5000)
