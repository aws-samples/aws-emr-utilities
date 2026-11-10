#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Recompute the totals and relative-cost tables from the per-query results in ../results.

Per query: median of the timed runs (SF10 has two runs per query, SF100 and SF1000 one).
Totals cover the queries that completed on both engines (Q64 is left out at SF10 and SF100).
Cost: billed vCPU-hours, memory GB-hours and storage GB-hours of whole jobs at approximate list rates,
used only as a ratio (Spark SQL job cost divided by DuckDB job cost).

Usage: python3 analyze_results.py [results_dir]
"""
import json
import os
import statistics as st
import sys

R = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
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
    excl = {64} if sf < 1000 else set()
    c1 = sorted((set(d) & set(s)) - excl)
    dd, ss = sum(d[q] for q in c1), sum(s[q] for q in c1)
    # Default-settings job cost at SF100 uses the reruns without Q64: the original jobs billed minutes
    # for a Q64 that ran until it failed with out of temporary disk space.
    rr = {8: "rerun2-same", 16: "rerun", 32: "rerun"}[v] if sf == 100 else None
    dcost = cost(load(f"{rr}/duckdb-sf100-d{v}.json")) if rr else cost(dk)
    print(f"SF{sf:<5}{v:>5}{dd:>10.1f}{ss:>10.1f}{ss / dd:>7.2f}{len(c1):>4}{cost(sp) / dcost:>7.2f}")
