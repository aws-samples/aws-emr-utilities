# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Diagnostic: why is one TPC-DS query slow on DuckDB over Apache Iceberg?

For each source (iceberg:<warehouse> and/or parquet:<prefix>) prints, with EXPL| prefixes:
  - DuckDB's own view of the machine (threads, memory_limit, temp directory limit) and the cgroup memory limit
  - EXPLAIN (estimated cardinalities)
  - EXPLAIN ANALYZE (actual operator timings and cardinalities) and wall time

Args: <s3_queries_json> <query_nr> <sources comma-separated>
"""
import json
import os
import re
import sys
import time

import duckdb

_SAFE = re.compile(r"[A-Za-z0-9_./:=@%+~-]+")
def _safe(v):
    """Accept only path characters (no quotes or semicolons) for values that reach DuckDB, even though they are
    passed as bound parameters wherever the API allows it."""
    if not _SAFE.fullmatch(v):
        raise SystemExit(f"Unexpected characters in argument: {v!r}")
    return v

QFILE, QNR, SOURCES = sys.argv[1], int(sys.argv[2]), [_safe(x) for x in sys.argv[3].split(",")]
EXT_DIR = os.path.join(os.getcwd(), "environment", "duckdb_extensions")
TABLES = ["call_center", "catalog_page", "catalog_returns", "catalog_sales", "customer",
          "customer_address", "customer_demographics", "date_dim", "household_demographics",
          "income_band", "inventory", "item", "promotion", "reason", "ship_mode", "store",
          "store_returns", "store_sales", "time_dim", "warehouse", "web_page", "web_returns",
          "web_sales", "web_site"]


def new_con():
    con = duckdb.connect(config={"extension_directory": EXT_DIR})
    for e in ("httpfs", "aws", "avro", "iceberg"):
        con.load_extension(e)
    con.execute("CREATE OR REPLACE SECRET (TYPE s3, PROVIDER credential_chain);")
    return con


def con_for(kind, path):
    con = new_con()
    for t in TABLES:
        if kind == "parquet":
            con.read_parquet(f"{path}/{t}/*.parquet").create_view(t)
        else:
            con.sql("SELECT * FROM iceberg_scan(?)", params=[f"{path}/tpcds/{t}"]).create_view(t)
    return con


def emit(tag, text):
    for ln in str(text).splitlines():
        print(f"EXPL|{tag}|{ln}", flush=True)


def cgroup():
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            return p, open(p).read().strip()
        except OSError:
            pass
    return None, None


boot = new_con()
txt = boot.execute("SELECT content FROM read_text(?)", [QFILE]).fetchone()[0]
SQL = json.loads(txt)[str(QNR)]
boot.close()
print(f"EXPL|INFO|duckdb={duckdb.__version__} query=Q{QNR} nproc={os.cpu_count()} cgroup={cgroup()}", flush=True)

for src in SOURCES:
    kind, path = src.split(":", 1)
    con = con_for(kind, path)
    emit(f"{kind}-SETTINGS", "; ".join(
        f"{k}={con.execute('SELECT current_setting(?)', [k]).fetchall()[0][0]}"
        for k in ("threads", "memory_limit", "max_temp_directory_size", "temp_directory")))
    t = time.perf_counter()
    plan = con.sql(SQL).explain()
    emit(f"{kind}-EXPLAIN", plan)
    print(f"EXPL|{kind}-EXPLAIN_SECONDS|{time.perf_counter() - t:.1f}", flush=True)
    t = time.perf_counter()
    an = con.sql(SQL).explain("analyze")
    wall = time.perf_counter() - t
    emit(f"{kind}-ANALYZE", an)
    print(f"EXPL|{kind}-ANALYZE_SECONDS|{wall:.1f}", flush=True)
    con.close()
print("EXPL|DONE|1", flush=True)
