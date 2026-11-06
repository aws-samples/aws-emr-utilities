# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate TPC-DS data with DuckDB's dsdgen (tpcds extension) and write Parquet to S3.

Runs on EMR Serverless as a pure-Python entry point (no SparkSession), same venv as the
other DuckDB jobs. The tpcds extension is shipped as a second archive (#tpcds_ext).

Args: <scale_factor> <s3_parquet_prefix>
Prints TPCDS_GEN_JSON={...} with per-table row counts and timings.
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

SF = str(float(sys.argv[1]) if "." in sys.argv[1] else int(sys.argv[1]))  # must be a number
OUT = _safe(sys.argv[2].rstrip("/"))
EXT_DIR = os.path.join(os.getcwd(), "environment", "duckdb_extensions")

con = duckdb.connect(config={"extension_directory": EXT_DIR})
for e in ("httpfs", "aws"):
    con.load_extension(e)
con.execute("CREATE OR REPLACE SECRET (TYPE s3, PROVIDER credential_chain);")
con.execute("LOAD './tpcds_ext/tpcds.duckdb_extension';")
print("DUCKDB_VERSION=" + duckdb.__version__, flush=True)

t0 = time.perf_counter()
con.execute("CALL dsdgen(sf=?)", [float(SF)])
gen_s = time.perf_counter() - t0
print(f"DSDGEN_SECONDS={gen_s:.1f}", flush=True)

tables = [r[0] for r in con.execute(
    "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY 1").fetchall()]
report = {"sf": SF, "duckdb": duckdb.__version__, "dsdgen_s": round(gen_s, 1), "tables": {}}
for t in tables:
    rows = con.table(t).aggregate("count(*)").fetchall()[0][0]
    t1 = time.perf_counter()
    con.execute(f"COPY {t} TO '{OUT}/{t}' (FORMAT parquet, COMPRESSION zstd, PER_THREAD_OUTPUT true, "
                f"OVERWRITE_OR_IGNORE true);")
    dt = time.perf_counter() - t1
    report["tables"][t] = {"rows": rows, "copy_s": round(dt, 1)}
    print(f"TABLE {t}: rows={rows} copy={dt:.1f}s", flush=True)

print("TPCDS_GEN_JSON=" + json.dumps(report), flush=True)
