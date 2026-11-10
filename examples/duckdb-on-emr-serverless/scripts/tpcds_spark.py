# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""TPC-DS on Spark SQL (EMR Serverless 7.14, Spark 3.5.8): the same 99 queries on the same Iceberg tables.

Same methodology as tpcds_duckdb.py: REPEAT timed runs per query (best reported), execute + collect,
per-run timeout via job-group cancellation, one TPCDS_Q={...} line per query.
The query cache is cleared before each run.

Args: <s3_warehouse> <s3_queries_json> [repeat=2] [timeout_s=900] [only=1,2,3]
"""
import json
import sys
import threading
import time

from pyspark.sql import SparkSession

WH = sys.argv[1].rstrip("/")
QFILE = sys.argv[2]
REPEAT = int(sys.argv[3]) if len(sys.argv) > 3 else 2
TIMEOUT = int(sys.argv[4]) if len(sys.argv) > 4 else 900
ONLY = [int(x) for x in sys.argv[5].split(",")] if len(sys.argv) > 5 and sys.argv[5] else None

spark = (
    SparkSession.builder.appName("tpcds-spark-sql")
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.demo", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.demo.type", "hadoop")
    .config("spark.sql.catalog.demo.warehouse", WH)
    .getOrCreate()
)
sc = spark.sparkContext
spark.sql("USE demo.tpcds")


def run_once(nr, sql, attempt):
    gid = f"q{nr}-{attempt}"
    sc.setJobGroup(gid, gid, interruptOnCancel=True)
    timer = threading.Timer(TIMEOUT, lambda: sc.cancelJobGroup(gid))
    timer.start()
    try:
        spark.catalog.clearCache()
        t = time.perf_counter()
        rows = len(spark.sql(sql).collect())
        return time.perf_counter() - t, rows, None
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace("\n", " ")[:300]
        return None, None, ("timeout" if "cancel" in msg.lower() else msg)
    finally:
        timer.cancel()


def main():
    txt = "".join(r.value for r in spark.read.text(QFILE, wholetext=True).collect())
    queries = {int(k): v for k, v in json.loads(txt).items()}
    print(f"SPARK_VERSION={spark.version} WAREHOUSE={WH} REPEAT={REPEAT} TIMEOUT={TIMEOUT}", flush=True)
    for nr in sorted(queries):
        if ONLY and nr not in ONLY:
            continue
        times, rows, err = [], None, None
        for a in range(REPEAT):
            dt, r, err = run_once(nr, queries[nr], a)
            if err:
                break
            times.append(dt); rows = r
        rec = {"q": nr, "engine": "spark", "best": round(min(times), 3) if times else None,
               "all": [round(x, 3) for x in times], "rows": rows, "error": err}
        print("TPCDS_Q=" + json.dumps(rec), flush=True)
    print("TPCDS_DONE=1", flush=True)
    spark.stop()


if __name__ == "__main__":
    main()
