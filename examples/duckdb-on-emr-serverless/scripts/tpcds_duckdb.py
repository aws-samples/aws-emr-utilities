# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""TPC-DS on DuckDB 2.0.0: run the 99 queries against Apache Iceberg tables on S3.

Methodology (same spirit as the single-table benchmark):
- A fresh DuckDB connection for every timed run, so no cache carries over between runs.
- REPEAT timed runs per query, report the best.
- Time covers execute + fetchall (Iceberg metadata reads from S3 included).
- Per-run timeout; queries that fail or time out are recorded, not retried.
- Extensions are pre-bundled in the venv (no runtime INSTALL).

Args: <s3_warehouse> <s3_queries_json> [repeat=2] [timeout_s=900] [only=1,2,3]
Each query prints one TPCDS_Q={...} line so partial results survive a failed job.
"""
import json
import os
import re
import sys
import threading
import time

import duckdb

_SAFE = re.compile(r"[A-Za-z0-9_./:=@%+~-]+")
def _safe(v):
    """Accept only path characters (no quotes or semicolons) for values that reach DuckDB, even though they are
    passed as bound parameters wherever the API allows it."""
    if not _SAFE.fullmatch(v):
        raise SystemExit(f"Unexpected characters in argument: {v!r}")
    return v

WH = _safe(sys.argv[1].rstrip("/"))
QFILE = sys.argv[2]
REPEAT = int(sys.argv[3]) if len(sys.argv) > 3 else 2
TIMEOUT = int(sys.argv[4]) if len(sys.argv) > 4 else 900
ONLY = [int(x) for x in sys.argv[5].split(",")] if len(sys.argv) > 5 and sys.argv[5] else None
EXT_DIR = os.path.join(os.getcwd(), "environment", "duckdb_extensions")
TABLES = ["call_center", "catalog_page", "catalog_returns", "catalog_sales", "customer",
          "customer_address", "customer_demographics", "date_dim", "household_demographics",
          "income_band", "inventory", "item", "promotion", "reason", "ship_mode", "store",
          "store_returns", "store_sales", "time_dim", "warehouse", "web_page", "web_returns",
          "web_sales", "web_site"]


def fresh_con():
    con = duckdb.connect(config={"extension_directory": EXT_DIR})
    for e in ("httpfs", "aws", "avro", "iceberg"):
        con.load_extension(e)
    con.execute("CREATE OR REPLACE SECRET (TYPE s3, PROVIDER credential_chain);")
    for t in TABLES:
        if WH.startswith("parquet:"):  # diagnostic mode: plain Parquet instead of Iceberg
            con.read_parquet(f"{WH[8:]}/{t}/*.parquet").create_view(t)
        else:
            con.sql("SELECT * FROM iceberg_scan(?)", params=[f"{WH}/tpcds/{t}"]).create_view(t)
    return con


def load_queries():
    con = duckdb.connect(config={"extension_directory": EXT_DIR})
    con.execute("LOAD httpfs;"); con.execute("LOAD aws;")
    con.execute("CREATE OR REPLACE SECRET (TYPE s3, PROVIDER credential_chain);")
    txt = con.execute("SELECT content FROM read_text(?)", [QFILE]).fetchone()[0]
    con.close()
    return {int(k): v for k, v in json.loads(txt).items()}


def run_once(sql):
    con = fresh_con()
    timer = threading.Timer(TIMEOUT, con.interrupt)
    timer.start()
    try:
        t = time.perf_counter()
        rows = len(con.execute(sql).fetchall())
        return time.perf_counter() - t, rows, None
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace("\n", " ")[:300]
        return None, None, ("timeout" if "nterrupt" in msg else msg)
    finally:
        timer.cancel()
        con.close()


def main():
    queries = load_queries()
    print(f"DUCKDB_VERSION={duckdb.__version__} WAREHOUSE={WH} REPEAT={REPEAT} TIMEOUT={TIMEOUT}", flush=True)
    for nr in sorted(queries):
        if ONLY and nr not in ONLY:
            continue
        times, rows, err = [], None, None
        for _ in range(REPEAT):
            dt, r, err = run_once(queries[nr])
            if err:
                break
            times.append(dt); rows = r
        rec = {"q": nr, "engine": "duckdb", "best": round(min(times), 3) if times else None,
               "all": [round(x, 3) for x in times], "rows": rows, "error": err}
        print("TPCDS_Q=" + json.dumps(rec), flush=True)
    print("TPCDS_DONE=1", flush=True)


if __name__ == "__main__":
    main()
