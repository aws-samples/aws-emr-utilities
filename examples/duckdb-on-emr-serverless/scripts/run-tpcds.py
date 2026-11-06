#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Submit the TPC-DS jobs to EMR Serverless, wait, and save per-query results.
Required env: EMR_APP_ID, EXEC_ROLE_ARN, S3_BUCKET. Optional: AWS_REGION, AWS_PROFILE, OUTDIR.

Labels:
  gen-sf<N>               DuckDB dsdgen -> Parquet on S3
  iceberg-sf<N>           Spark: Parquet -> Apache Iceberg tables
  duckdb-sf<N>-d<V>       DuckDB 2.0.0, one V-vCPU driver, 99 queries
  spark-sf<N>-<V>         Spark SQL, V total vCPUs as 4-vCPU executors, 99 queries
Options (env): REPEAT (default 2), TIMEOUT (default 900), ONLY ("1,2,3")

Usage: python3 run-tpcds.py gen-sf1 iceberg-sf1 duckdb-sf1-d8 spark-sf1-8
Labels given on one command line run IN PARALLEL; chain separate invocations for dependencies.
If credentials expire, the runner stops polling and prints the job run id; attach again with label@jobRunId.
"""
import concurrent.futures as cf
import gzip
import json
import os
import re
import sys
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

def _required(name):
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Set {name} (see README.md, section 'Configure')")
    return v
APP_ID = _required("EMR_APP_ID")                 # EMR Serverless application id (Spark, emr-7.14.0, x86_64)
EXEC_ROLE = _required("EXEC_ROLE_ARN")           # job runtime role ARN (S3 read/write, logs)
BUCKET = _required("S3_BUCKET").rstrip("/")      # s3://<bucket> holding envs/, scripts/, tpcds/, logs/
REGION = os.environ.get("AWS_REGION", "us-east-1")
HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, os.environ.get("OUTDIR", "results"))
os.makedirs(RES, exist_ok=True)
ENV_ARCHIVE = f"{BUCKET}/envs/duckdb20_env.tar.gz#environment"
EXT_ARCHIVE = f"{BUCKET}/envs/tpcds_ext.tar.gz#tpcds_ext"
QUERIES = f"{BUCKET}/tpcds/queries-duckdb.json"
QUERIES_SPARK = f"{BUCKET}/tpcds/queries-spark.json"
ICEBERG_JARS = "--conf spark.jars=/usr/share/aws/iceberg/lib/iceberg-spark3-runtime.jar"
DUCK = {4: ("14g", 16, None), 8: ("30g", 32, "100g"), 16: ("60g", 64, "100g"), 32: ("109g", 120, "200g")}
REPEAT = os.environ.get("REPEAT", "2")
TIMEOUT = os.environ.get("TIMEOUT", "900")
ONLY = os.environ.get("ONLY", "")
PROFILE = os.environ.get("AWS_PROFILE")                   # optional; default credential chain if unset


def parquet(sf): return f"{BUCKET}/tpcds/parquet/sf{sf}"
def warehouse(sf): return f"{BUCKET}/tpcds/warehouse_sf{sf}"


def duck_params(script, args, vcpu, disk=None):
    mem, mem_gb, d = DUCK[vcpu]
    conf = [f"spark.archives={ENV_ARCHIVE},{EXT_ARCHIVE}",
            "spark.emr-serverless.driverEnv.PYSPARK_DRIVER_PYTHON=./environment/bin/python",
            "spark.emr-serverless.driverEnv.PYSPARK_PYTHON=./environment/bin/python",
            "spark.executorEnv.PYSPARK_PYTHON=./environment/bin/python",
            "spark.executor.instances=1", f"spark.driver.cores={vcpu}", f"spark.driver.memory={mem}"]
    if disk or d:
        conf.append(f"spark.emr-serverless.driver.disk={disk or d}")
    return {"sparkSubmit": {"entryPoint": f"{BUCKET}/scripts/{script}", "entryPointArguments": args,
                            "sparkSubmitParameters": " ".join(f"--conf {c}" for c in conf)}}


def spark_params(script, args, total_vcpu, extra=()):
    ex = max(total_vcpu // 4, 1)
    conf = [f"spark.executor.instances={ex}", "spark.dynamicAllocation.enabled=false",
            "spark.executor.cores=4", "spark.executor.memory=14g", "spark.executor.disk=100g",
            "spark.driver.cores=2", "spark.driver.memory=8g", *extra]
    return {"sparkSubmit": {"entryPoint": f"{BUCKET}/scripts/{script}", "entryPointArguments": args,
                            "sparkSubmitParameters": ICEBERG_JARS + " " + " ".join(f"--conf {c}" for c in conf)}}


def job_for(label):
    m = re.fullmatch(r"gen-sf(\d+)", label)
    if m:
        sf = m.group(1)
        return duck_params("tpcds_gen.py", [sf, parquet(sf)], 32 if int(sf) >= 100 else 8)
    m = re.fullmatch(r"iceberg-sf(\d+)", label)
    if m:
        sf = m.group(1)
        return spark_params("tpcds_to_iceberg.py", [parquet(sf), warehouse(sf)], 32)
    m = re.fullmatch(r"duckdb-sf(\d+)-d(\d+)", label)
    if m:
        sf, v = m.group(1), int(m.group(2))
        return duck_params("tpcds_duckdb.py", [warehouse(sf), QUERIES, REPEAT, TIMEOUT, ONLY], v)
    m = re.fullmatch(r"txticeberg-sf(\d+)", label)  # SF1000: AWS public text files -> Iceberg, 128 vCPU of executors
    if m:
        sf = m.group(1)
        return spark_params("tpcds_text_to_iceberg.py", [f"{BUCKET}/tpcds/text/sf{sf}", warehouse(sf), f"{BUCKET}/tpcds/tpcds-schema.json"], 128)
    m = re.fullmatch(r"stats-sf(\d+)", label)  # tuned mode, step 1: Iceberg table statistics for Spark
    if m:
        return spark_params("tpcds_spark_stats.py", [warehouse(m.group(1))], 32)
    m = re.fullmatch(r"sparkT-sf(\d+)-(\d+)", label)  # tuned Spark: Iceberg stats + cost-based optimizer
    if m:
        sf, v = m.group(1), int(m.group(2))
        return spark_params("tpcds_spark.py", [warehouse(sf), QUERIES_SPARK, REPEAT, TIMEOUT, ONLY], v,
                            extra=("spark.sql.cbo.enabled=true", "spark.sql.cbo.joinReorder.enabled=true"))
    m = re.fullmatch(r"probe-sf(\d+)", label)  # diagnostic: what stats can Spark use on Iceberg
    if m:
        return spark_params("tpcds_cbo_probe.py", [warehouse(m.group(1))], 8)
    m = re.fullmatch(r"explain-sf(\d+)-d(\d+)", label)  # diagnostic: EXPLAIN / EXPLAIN ANALYZE of one query
    if m:
        sf, v = m.group(1), int(m.group(2))
        srcs = ",".join({"iceberg": "iceberg:" + warehouse(sf), "parquet": "parquet:" + parquet(sf)}[k]
                        for k in os.environ.get("EXPL_SOURCES", "iceberg,parquet").split(","))
        return duck_params("tpcds_explain.py", [QUERIES, os.environ.get("EXPL_QNR", "19"), srcs], v)
    m = re.fullmatch(r"duckdbpq-sf(\d+)-d(\d+)", label)  # diagnostic: DuckDB on plain Parquet
    if m:
        sf, v = m.group(1), int(m.group(2))
        return duck_params("tpcds_duckdb.py", ["parquet:" + parquet(sf), QUERIES, REPEAT, TIMEOUT, ONLY], v)
    m = re.fullmatch(r"spark-sf(\d+)-(\d+)", label)
    if m:
        sf, v = m.group(1), int(m.group(2))
        # at SF1000 shuffle/spill can exceed the default 20 GB executor disk, so give executors the 200 GB maximum
        extra = ("spark.emr-serverless.executor.disk=200g",) if int(sf) >= 1000 else ()
        return spark_params("tpcds_spark.py", [warehouse(sf), QUERIES_SPARK, REPEAT, TIMEOUT, ONLY], v, extra=extra)
    raise SystemExit(f"unknown label {label}")


_SESSION = boto3.Session(profile_name=PROFILE, region_name=REGION)
EMR = _SESSION.client("emr-serverless")
S3 = _SESSION.client("s3")
_LOG_BUCKET, _, _LOG_PREFIX = BUCKET[len("s3://"):].partition("/")


def call(fn, **kwargs):
    """Call an AWS API; turn SDK errors into RuntimeError (expired credentials get a clear message)."""
    try:
        return fn(**kwargs)
    except ClientError as e:
        if "ExpiredToken" in e.response.get("Error", {}).get("Code", ""):
            raise RuntimeError("AWS credentials expired. Refresh them (or use a profile that refreshes itself) "
                               "and run the label again with label@jobRunId.") from e
        raise RuntimeError(str(e)[:500]) from e
    except BotoCoreError as e:
        raise RuntimeError(str(e)[:500]) from e


def driver_log(rid, name):
    """Return the text of a driver log (stdout.gz or stderr.gz) of a job run."""
    key = "/".join(p for p in (_LOG_PREFIX, "logs", "applications", APP_ID, "jobs", rid, "SPARK_DRIVER", name) if p)
    body = call(S3.get_object, Bucket=_LOG_BUCKET, Key=key)["Body"].read()
    return gzip.decompress(body).decode("utf-8", errors="replace")


def run(label):
    rid = None
    if "@" in label:  # attach to an already-submitted job run: label@jobRunId
        label, rid = label.split("@", 1)
    if rid is None:
        cfg = job_for(label)
        jr = call(EMR.start_job_run, applicationId=APP_ID, executionRoleArn=EXEC_ROLE, name=f"tpcds-{label}",
                  executionTimeoutMinutes=int(os.environ.get("EXEC_TIMEOUT_MIN", "240")), jobDriver=cfg,
                  configurationOverrides={"monitoringConfiguration": {"s3MonitoringConfiguration": {"logUri": f"{BUCKET}/logs/"}}})
        rid = jr["jobRunId"]
        print(f"[{label}] submitted {rid}", flush=True)
    else:
        print(f"[{label}] attached to {rid}", flush=True)
    while True:
        try:
            j = call(EMR.get_job_run, applicationId=APP_ID, jobRunId=rid)["jobRun"]
        except RuntimeError as e:
            if "credentials expired" in str(e):
                print(f"[{label}] {e} Job run id: {rid}", flush=True)
                return
            print(f"[{label}] poll error (retrying): {e}", flush=True); time.sleep(30); continue
        if j["state"] in ("SUCCESS", "FAILED", "CANCELLED"):
            break
        time.sleep(30)
    for _ in range(12):  # billing data can appear a little after the job ends
        if j.get("billedResourceUtilization"):
            break
        time.sleep(10)
        try:
            j = call(EMR.get_job_run, applicationId=APP_ID, jobRunId=rid)["jobRun"]
        except RuntimeError:
            pass
    print(f"[{label}] {j['state']} in {j.get('totalExecutionDurationSeconds')}s {j.get('stateDetails','')[:200]}", flush=True)
    qs, extra, misc, full = [], {}, [], []
    try:
        for ln in driver_log(rid, "stdout.gz").splitlines():
            ln = ln.rstrip()
            if ln.startswith(("EXPL|", "PROBE|")):
                full.append(ln)
            if ln.startswith("TPCDS_Q="):
                qs.append(json.loads(ln[8:]))
            elif ln.startswith(("TPCDS_GEN_JSON=", "TPCDS_ICEBERG_JSON=")):
                k, v = ln.split("=", 1); extra[k] = json.loads(v)
            elif ln.startswith(("DUCKDB_VERSION", "SPARK_VERSION", "DSDGEN", "TABLE ", "ICEBERG ", "STATS ")):
                misc.append(ln[:300])
    except Exception as e:  # noqa: BLE001
        misc.append(f"STDOUT_FETCH_FAILED: {e}")
    if full:
        with open(os.path.join(RES, f"{label}.stdout.txt"), "w") as fh:
            fh.write("\n".join(full))
    if j["state"] != "SUCCESS":  # surface the driver error tail
        try:
            misc.append("STDERR_TAIL: " + " | ".join(driver_log(rid, "stderr.gz").splitlines()[-6:])[:900])
        except Exception:  # noqa: BLE001
            pass
    rec = {"label": label, "job_run_id": rid, "state": j["state"], "details": j.get("stateDetails"),
           "duration_s": j.get("totalExecutionDurationSeconds"), "billed": j.get("billedResourceUtilization"),
           "queries": qs, "extra": extra, "misc": misc, "repeat": os.environ.get("REPEAT_USED", REPEAT), "timeout": TIMEOUT}
    with open(os.path.join(RES, f"{label}.json"), "w") as fh:
        json.dump(rec, fh, indent=1, default=str)
    ok = sum(1 for q in qs if not q["error"])
    print(f"[{label}] saved: {len(qs)} queries ({ok} ok)  billed={rec['billed']}", flush=True)
    for m_ in misc[-3:]:
        print(f"    {m_}")
    return rec


if __name__ == "__main__":
    labels = sys.argv[1:]
    with cf.ThreadPoolExecutor(max_workers=max(len(labels), 1)) as ex:
        list(ex.map(run, labels))
