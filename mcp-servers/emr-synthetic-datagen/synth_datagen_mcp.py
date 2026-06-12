#!/usr/bin/env python3
"""
EMR Synthetic Data Generator — MCP Server

Reverse-engineers synthetic datasets from SQL queries + event logs so a
production job can be replicated in a test EMR environment without customer
data (declarative column rules, shared ID pools for join realism,
volume/skew targets from the event log).

Tools:
  analyze_sql_structure       — tables, join keys, exploded maps, window keys
  analyze_event_log_profile   — volumes, scan signatures, shuffle profile
  build_dataset_spec          — SQL + event log (+ DDLs) → dataset spec JSON
  generate_datagen_script     — spec → runnable PySpark generator script
  generate_table_ddl          — spec → CREATE EXTERNAL TABLE statements
  run_datagen_on_emr          — submit the generated script to EMR Serverless
  check_job_status            — poll a submitted job

Configuration via environment variables (only needed for run_datagen_on_emr):
    EMR_SERVERLESS_APP_ID:  EMR Serverless application ID
    EMR_EXECUTION_ROLE:     IAM role ARN for EMR Serverless jobs
    ARTIFACTS_S3_PATH:      S3 prefix for uploading generated scripts
    AWS_REGION:             AWS region (default: us-east-1)

Local file paths and s3:// paths are both accepted for inputs.
"""
import json
import os
import logging

from mcp.server.fastmcp import FastMCP

import synth_datagen as core

log = logging.getLogger("synth-datagen-mcp")

APP_ID = os.environ.get("EMR_SERVERLESS_APP_ID", "")
EXEC_ROLE = os.environ.get("EMR_EXECUTION_ROLE", "")
ARTIFACTS = os.environ.get("ARTIFACTS_S3_PATH", "").rstrip("/")
REGION = os.environ.get("AWS_REGION", "us-east-1")

mcp = FastMCP("emr-synthetic-datagen")

_SPECS = {}  # in-session spec store: name -> spec dict


def _read(path):
    """Read a local or s3:// text file."""
    if path.startswith("s3://"):
        import boto3
        s3 = boto3.client("s3", region_name=REGION)
        bucket, key = path.replace("s3://", "").split("/", 1)
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    with open(path) as f:
        return f.read()


@mcp.tool()
def analyze_sql_structure(sql_path: str) -> str:
    """Extract structural signals from a Spark SQL file: referenced tables,
    join conditions, LATERAL VIEW EXPLODE map columns, window partition keys,
    and date-filter columns. sql_path may be local or s3://."""
    info = core.analyze_sql(_read(sql_path))
    return json.dumps(info, indent=1)


@mcp.tool()
def analyze_event_log_profile(extract_path: str) -> str:
    """Profile a task_stage_summary extract (Config Advisor extractor output):
    total input/shuffle volumes, per-signature scan stages (for table volume
    attribution), peak stage shuffle, duration, executor count.
    extract_path may be local or s3://."""
    profile = core.analyze_event_log(json.loads(_read(extract_path)))
    return json.dumps(profile, indent=1)


@mcp.tool()
def build_dataset_spec(sql_path: str, extract_path: str = "",
                       ddl_paths: str = "", scale: float = 1.0,
                       sent_date: str = "2026-06-01",
                       spec_name: str = "default") -> str:
    """Build a synthetic dataset SPEC from a SQL file plus (optionally) an
    event-log extract and table DDLs.

    - sql_path: the production query (local or s3://)
    - extract_path: task_stage_summary JSON for volume/skew calibration
    - ddl_paths: comma-separated CREATE TABLE files for exact schemas
    - scale: fraction of production volume (1.0 = match event log, 0.066 ≈ 1/15)
    - spec_name: handle for follow-up generate_* calls in this session

    Returns a summary; the full spec is stored under spec_name. Edit rules by
    calling this again or post-processing the JSON from get_dataset_spec."""
    extract = json.loads(_read(extract_path)) if extract_path else None
    ddls = [_read(p.strip()) for p in ddl_paths.split(",") if p.strip()]
    spec = core.build_spec(_read(sql_path), extract, ddls=ddls, scale=scale,
                           sent_date=sent_date)
    _SPECS[spec_name] = spec
    summary = {
        "spec_name": spec_name,
        "tables": [{"name": t["name"], "rows": t["rows"],
                    "target_gb": t["target_gb"],
                    "partition_col": t["partition_col"],
                    "n_columns": len(t["columns"])} for t in spec["tables"]],
        "id_pools": spec["id_pools"],
        "total_gb": round(sum(t["target_gb"] for t in spec["tables"]), 1),
        "source_profile": spec["source_profile"],
    }
    return json.dumps(summary, indent=1)


@mcp.tool()
def get_dataset_spec(spec_name: str = "default") -> str:
    """Return the full dataset spec JSON built by build_dataset_spec."""
    if spec_name not in _SPECS:
        return json.dumps({"error": "no spec named %r; call build_dataset_spec first" % spec_name})
    return json.dumps(_SPECS[spec_name], indent=1)


@mcp.tool()
def generate_datagen_script(spec_name: str = "default",
                            data_root: str = "s3://CHANGE-ME/synth",
                            save_to: str = "") -> str:
    """Generate the runnable PySpark data-generator script from a spec.
    data_root: where the script writes parquet (s3:// or hdfs://).
    save_to: optional local/s3 path to write the script; otherwise returned inline."""
    if spec_name not in _SPECS:
        return json.dumps({"error": "no spec named %r" % spec_name})
    script = core.generate_pyspark_script(_SPECS[spec_name], data_root)
    if save_to:
        _write(save_to, script)
        return json.dumps({"saved": save_to, "bytes": len(script)})
    return script


@mcp.tool()
def generate_table_ddl(spec_name: str = "default",
                       data_root: str = "s3://CHANGE-ME/synth",
                       save_to: str = "") -> str:
    """Generate CREATE EXTERNAL TABLE DDL (Hive/Glue) over the generated data."""
    if spec_name not in _SPECS:
        return json.dumps({"error": "no spec named %r" % spec_name})
    ddl = core.generate_ddl(_SPECS[spec_name], data_root)
    if save_to:
        _write(save_to, ddl)
        return json.dumps({"saved": save_to, "bytes": len(ddl)})
    return ddl


def _write(path, content):
    if path.startswith("s3://"):
        import boto3
        s3 = boto3.client("s3", region_name=REGION)
        bucket, key = path.replace("s3://", "").split("/", 1)
        s3.put_object(Bucket=bucket, Key=key, Body=content.encode())
    else:
        with open(path, "w") as f:
            f.write(content)


@mcp.tool()
def run_datagen_on_emr(spec_name: str = "default",
                       data_root: str = "",
                       executors: int = 12,
                       job_name: str = "synth-datagen") -> str:
    """Generate the script for spec_name, upload it to ARTIFACTS_S3_PATH, and
    submit it to the configured EMR Serverless application. Requires env:
    EMR_SERVERLESS_APP_ID, EMR_EXECUTION_ROLE, ARTIFACTS_S3_PATH."""
    if not (APP_ID and EXEC_ROLE and ARTIFACTS):
        return json.dumps({"error": "EMR_SERVERLESS_APP_ID / EMR_EXECUTION_ROLE / "
                                    "ARTIFACTS_S3_PATH not configured"})
    if spec_name not in _SPECS:
        return json.dumps({"error": "no spec named %r" % spec_name})
    if not data_root:
        return json.dumps({"error": "data_root is required (s3://...)"})
    import boto3
    script = core.generate_pyspark_script(_SPECS[spec_name], data_root)
    script_path = "%s/%s.py" % (ARTIFACTS, job_name)
    _write(script_path, script)
    emr = boto3.client("emr-serverless", region_name=REGION)
    resp = emr.start_job_run(
        applicationId=APP_ID,
        executionRoleArn=EXEC_ROLE,
        name=job_name,
        jobDriver={"sparkSubmit": {
            "entryPoint": script_path,
            "entryPointArguments": ["--output", data_root],
            "sparkSubmitParameters":
                "--conf spark.executor.cores=4 --conf spark.executor.memory=16G "
                "--conf spark.dynamicAllocation.maxExecutors=%d" % executors,
        }})
    return json.dumps({"job_run_id": resp["jobRunId"], "script": script_path,
                       "application_id": APP_ID})


@mcp.tool()
def check_job_status(job_run_id: str) -> str:
    """Poll a submitted EMR Serverless datagen job."""
    if not APP_ID:
        return json.dumps({"error": "EMR_SERVERLESS_APP_ID not configured"})
    import boto3
    emr = boto3.client("emr-serverless", region_name=REGION)
    jr = emr.get_job_run(applicationId=APP_ID, jobRunId=job_run_id)["jobRun"]
    return json.dumps({"state": jr["state"], "details": jr.get("stateDetails", "")})


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "stdio"))
