# DuckDB 2.0.0 runs (history)

These are the DuckDB 2.0.0 results that the published comparison used before DuckDB 2.0.1. They were run between
27 and 29 October 2026 with the same scripts, tables, job settings and default DuckDB settings as the 2.0.1 runs in
`../`. The Spark SQL runs in `../` and `../sf1000/` are the baseline for both DuckDB versions and were not rerun.
The bucket name is replaced by `amzn-s3-demo-bucket`.

| Path | Content |
|---|---|
| `duckdb-sf10-d*.json`, `duckdb-sf100-d*.json`, `sf1000/duckdb-sf1000-d32.json` | default DuckDB 2.0.0 runs, superseded by the 2.0.1 files with the same names in `../` and `../sf1000/` |
| `rerun/`, `rerun2-same/` | SF100 DuckDB 2.0.0 reruns without Q64, used for the 2.0.0 SF100 cost ratios |
| `rerun2-big/` | SF100 rerun with a larger memory setting, not used in the ratios |
| `duckdbpq-sf100-d32.json`, `diag/` | DuckDB 2.0.0 on plain Parquet instead of Iceberg |
| `explain/` | EXPLAIN output for the slow queries Q19 and Q64 |
| `summaries/` | summary files from the first analysis |

## Queries that did not complete on DuckDB 2.0.0

| Scale | Setting | Query | Error |
|---|---|---|---|
| SF100, 8 / 16 / 32 vCPU | default | Q64 | out of temporary directory space (limit 84.3 GiB at 100 GB of disk, 172.9 GiB at 200 GB) |
| SF1000, 32 vCPU | default | Q19, Q64, Q85 | out of temporary directory space (172.9 GiB limit at 200 GB of disk) |

DuckDB 2.0.1 completed all of these queries with the same settings.

## 2.0.0 summary

This is the table the README showed for DuckDB 2.0.0. It left Q64 out at SF10 and SF100 and, at SF100, took the
DuckDB job cost from the reruns without Q64 (`rerun2-same` for 8 vCPU, `rerun` for 16 and 32 vCPU), because the
original jobs billed several minutes for a Q64 that ran until it failed. The current `scripts/analyze_results.py`
compares all 99 queries and uses each job's own cost, so it does not reproduce this table from these files.

```
scale   vCPU  DuckDB s   Spark s  ratio   n   cost
SF10       8     147.4     531.5   3.61  98   1.73
SF10      16     127.9     390.3   3.05  98   1.23
SF10      32     112.4     326.9   2.91  98   1.11
SF100      8     824.7    2167.9   2.63  98   2.48
SF100     16     413.7    1275.3   3.08  98   2.17
SF100     32     280.9     842.0   3.00  98   1.64
SF1000    32    2544.7    6068.0   2.38  96   1.40
```

With 2.0.0, a repeat of the SF100 DuckDB run on 32 vCPU was 16% slower in total, and on 16 vCPU it differed by less
than 1%. The SF100 job for 8 vCPU failed once with an out-of-memory kill before the rerun that succeeded.
