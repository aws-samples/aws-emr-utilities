# Rerun in a new account (DuckDB 2.0.0 reproducibility check)

Per-query results from a second full run of the scripts in this folder with DuckDB 2.0.0, made in a new, empty AWS
account with the README steps. It checks that the DuckDB 2.0.0 results in `../results/duckdb-2.0.0/` can be
reproduced; DuckDB 2.0.1 was not run here. The bucket name is replaced by `amzn-s3-demo-bucket`.

Same layout as `../results`. From the package root, `python3 scripts/analyze_results.py results-clean-account-rerun`
prints a summary table. That script now compares all queries completed on both engines and uses each job's own
cost. With the method used for the 2.0.0 results (Q64 left out at SF10 and SF100, SF100 DuckDB cost from the reruns
without Q64, see `../results/duckdb-2.0.0/README.md`), this folder gives:

```
scale   vCPU  DuckDB s   Spark s  ratio   n   cost
SF10       8     139.1     522.6   3.76  98   1.79
SF10      16     117.0     374.3   3.20  98   1.28
SF10      32     107.4     324.8   3.02  98   1.15
SF100      8     818.7    2193.5   2.68  98   2.49
SF100     16     410.7    1255.0   3.06  98   2.15
SF100     32     279.9     794.0   2.84  98   1.69
SF1000    32    2417.9    5755.3   2.38  96   1.38
```

- `failed/`: DuckDB jobs at SF100 (8 and 16 vCPU) that were killed for exceeding memory (exit 137) after 18 queries.
  Each succeeded on retry.
- `rerun/` and `rerun2-same/`: SF100 DuckDB runs without Q64. The 16 vCPU entry in `rerun/` is a copy of the
  `rerun2-same` one.
- `tuned/sparkT-sf100-8.json`: tuned Spark, cancelled after 70 queries (every query after the first timeout timed out).
- `duckdb-sf100-d16.json` is a copy of `rerun2-same/duckdb-sf100-d16.json`, because the original default-settings
  job for that size was killed (see `failed/`). Totals exclude Q64 either way.
