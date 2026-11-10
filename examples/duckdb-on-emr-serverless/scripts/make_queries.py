#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Write queries-duckdb.json and queries-spark.json from the TPC-DS queries shipped with the DuckDB `tpcds` extension.

Spark SQL gets the same text, except double-quoted column aliases become backticks
(queries 16, 32, 50, 62, 92, 94, 95 and 99).

Usage: python3 make_queries.py [output_dir]
Needs internet once, to INSTALL the tpcds extension.
"""
import json
import os
import re
import sys

import duckdb

out = sys.argv[1] if len(sys.argv) > 1 else "."
con = duckdb.connect()
con.execute("INSTALL tpcds; LOAD tpcds;")
q = {str(n): s for n, s in con.execute("SELECT query_nr, query FROM tpcds_queries() ORDER BY query_nr").fetchall()}
assert len(q) == 99
spark = {k: re.sub(r'AS\s+"([^"]+)"', lambda m: "AS `" + m.group(1) + "`", v) for k, v in q.items()}
print("queries changed for Spark SQL:", sorted(int(k) for k in q if q[k] != spark[k]))
os.makedirs(out, exist_ok=True)
json.dump(q, open(os.path.join(out, "queries-duckdb.json"), "w"))
json.dump(spark, open(os.path.join(out, "queries-spark.json"), "w"))
