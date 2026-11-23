#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Recompute the totals and relative-cost tables from the per-query results in ../results.

Per query: median of the timed runs (SF10 has two runs per query, SF100 and SF1000 one).
Totals cover the queries that completed on both engines (all 99 with DuckDB 2.0.1).
Cost: billed vCPU-hours, memory GB-hours and storage GB-hours of whole jobs at approximate list rates,
used only as a ratio (Spark SQL job cost divided by DuckDB job cost).

With --repeat, also compare the SF100 DuckDB runs with the repeat in results/repeat/.

Usage: python3 analyze_results.py [--repeat] [results_dir]
"""
import json
import os
import statistics as st
import sys

ARGS = [a for a in sys.argv[1:] if a != "--repeat"]
REPEAT = "--repeat" in sys.argv[1:]
R = ARGS[0] if ARGS else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
RATE = (0.052, 0.0057, 0.000111)  # per vCPU-hour, per GB-hour of memory, per GB-hour of storage (approximate)


def load(p):
    return json.load(open(os.path.join(R, p)))


def per_q(d):
    return {q["q"]: st.median(q["all"]) for q in d["queries"] if not q.get("error") and q.get("all")}


def cost(d):
    b = d["billed"]
    return b["vCPUHour"] * RATE[0] + b["memoryGBHour"] * RATE[1] + b.get("storageGBHour", 0) * RATE[2]


runs = [(10, 8), (10, 16), (10, 32), (100, 8), (100, 16), (100, 32), (1000, 32)]
print(f"{'scale':7}{'vCPU':>5}{'DuckDB s':>10}{'Spark s':>10}{'ratio':>7}{'n':>4}{'cost':>7}")
for sf, v in runs:
    sub = "sf1000/" if sf == 1000 else ""
    dk, sp = load(f"{sub}duckdb-sf{sf}-d{v}.json"), load(f"{sub}spark-sf{sf}-{v}.json")
    d, s = per_q(dk), per_q(sp)
    c1 = sorted(set(d) & set(s))
    dd, ss = sum(d[q] for q in c1), sum(s[q] for q in c1)
    print(f"SF{sf:<5}{v:>5}{dd:>10.1f}{ss:>10.1f}{ss / dd:>7.2f}{len(c1):>4}{cost(sp) / cost(dk):>7.2f}")

if REPEAT:
    print()
    print("SF100 DuckDB repeat (results/repeat/): total query time, original vs repeat")
    print(f"{'vCPU':>4}{'original s':>12}{'repeat s':>10}{'diff':>8}{'n':>4}  rows")
    for v in (8, 16, 32):
        a, b = load(f"duckdb-sf100-d{v}.json"), load(f"repeat/duckdb-sf100-d{v}.json")
        d, r = per_q(a), per_q(b)
        c1 = sorted(set(d) & set(r))
        dd, rr = sum(d[q] for q in c1), sum(r[q] for q in c1)
        ra, rb = {q["q"]: q["rows"] for q in a["queries"]}, {q["q"]: q["rows"] for q in b["queries"]}
        rows = "identical" if ra == rb else "differ"
        print(f"{v:>4}{dd:>12.1f}{rr:>10.1f}{(rr / dd - 1) * 100:>+7.1f}%{len(c1):>4}  {rows}")
