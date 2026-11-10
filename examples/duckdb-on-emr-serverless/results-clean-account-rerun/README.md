# Rerun in a new account

Per-query results from a second full run of the scripts in this folder, made in a new, empty AWS account
with the README steps. The bucket name is replaced by `amzn-s3-demo-bucket`.

Same layout as `../results`. From this folder, run
`python3 scripts/analyze_results.py results-clean-account-rerun` to print the summary table.

- `failed/`: DuckDB jobs at SF100 (8 and 16 vCPU) that were killed for exceeding memory (exit 137) after 18 queries.
  Each succeeded on retry.
- `rerun/` and `rerun2-same/`: SF100 DuckDB runs without Q64. The 16 vCPU entry in `rerun/` is a copy of the
  `rerun2-same` one.
- `tuned/sparkT-sf100-8.json`: tuned Spark, cancelled after 70 queries (every query after the first timeout timed out).
- `duckdb-sf100-d16.json` is a copy of `rerun2-same/duckdb-sf100-d16.json`, because the original default-settings
  job for that size was killed (see `failed/`). Totals exclude Q64 either way.
