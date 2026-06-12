# EMR Synthetic Data Generator — MCP Server

Reverse-engineers **synthetic datasets** from a Spark SQL query + an event-log
extract, so a production job can be replicated in a test EMR environment with
zero customer data. Spec-first: column rules are driven by *observed*
production volumes and the query's structure instead of hand-written
generators.

Validated on a real workload migration case where this exact flow — SQL query
+ failed-job event log → synthetic dataset → clean-room driver — reproduced a
production shuffle-congestion failure at 1/15 scale.

## How it works

```
SQL query ─────────► analyze_sql_structure ──┐   tables, join keys, exploded
                                             │   maps, window keys
event-log extract ─► analyze_event_log ──────┼─► build_dataset_spec ─► SPEC (JSON)
                     volumes, scan sigs,     │
table DDLs ────────► exact schemas ──────────┘
                                                      SPEC
                                          ┌────────────┼─────────────┐
                                          ▼            ▼             ▼
                              generate_datagen   generate_table   run_datagen
                              _script (PySpark)  _ddl (Hive/Glue) _on_emr
```

Key realism features:
- **Shared ID pools** — join-key columns across tables draw from the same hash
  space, producing realistic join hit rates (`cs.visit_id = cks.visit_id` works)
- **Skew rules** — `hot_pct`/`hot_share` on ID pools reproduce hot-key skew
  (e.g. 2% of device ids owning 40% of events → window-function skew)
- **NULL-map rules** — `map_of_ids` with `null_pct: 95` reproduces the
  sparse-map patterns that change LATERAL VIEW EXPLODE row counts
- **Volume calibration** — per-table GB targets attributed from the event
  log's scan-stage signatures; `scale` shrinks everything proportionally
- **Partition windows** — date-partitioned tables generated day-by-day around
  a target date (the ±N-day scan window pattern)

## Tools

| Tool | Purpose |
|---|---|
| `analyze_sql_structure` | Tables, joins, explodes, window keys from SQL |
| `analyze_event_log_profile` | Volumes/scan signatures from task_stage_summary JSON |
| `build_dataset_spec` | Combine the above (+ DDLs, scale) into a spec |
| `get_dataset_spec` | Dump the full spec for inspection/editing |
| `generate_datagen_script` | Spec → runnable PySpark generator |
| `generate_table_ddl` | Spec → CREATE EXTERNAL TABLE (Hive/Glue) |
| `run_datagen_on_emr` | Upload + submit the generator to EMR Serverless |
| `check_job_status` | Poll the submitted job |

## Setup

```json
{
  "mcpServers": {
    "emr-synthetic-datagen": {
      "command": "python3",
      "args": ["/path/to/mcp-servers/emr-synthetic-datagen/synth_datagen_mcp.py"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "AWS_REGION": "us-east-1",
        "EMR_SERVERLESS_APP_ID": "<app id>",            // only for run_datagen_on_emr
        "EMR_EXECUTION_ROLE": "<role arn>",             // only for run_datagen_on_emr
        "ARTIFACTS_S3_PATH": "s3://bucket/synth-artifacts"
      }
    }
  }
}
```

`pip install -r requirements.txt` (mcp, boto3).

## Typical session

```
You: "Replicate the job from this SQL and event log at 1/15 scale"
  → build_dataset_spec(sql_path=..., extract_path=..., scale=0.066)
  → generate_datagen_script(data_root="s3://test-bucket/synth")
  → run_datagen_on_emr(data_root="s3://test-bucket/synth")
  → generate_table_ddl(...)   # run via spark-sql to register Glue tables
  → submit the original SQL (or a clean-room driver) against the tables
```

The core engine also works standalone without MCP:

```bash
python3 synth_datagen.py \
  --sql query.sql \
  --event-log-extract task_stage_summary/app.json \
  --ddl table1_ddl.sql --ddl table2_ddl.sql \
  --scale 0.066 --data-root s3://bucket/synth
# → dataset_spec.json, generated_datagen.py, generated_tables.sql
```

## Limitations

- Tables referenced through **template placeholders**
  (`FROM communications.{engagement_base}`) are invisible to the SQL parser —
  resolve placeholders first or add those tables to the spec manually.
- Volume attribution maps the largest scan signatures to tables in FROM-clause
  order — verify per-table `target_gb` in the spec and adjust before generating
  at full scale.
- Column rules are heuristic (name/type-based). Review the spec — especially
  `cardinality`, `hot_pct`, `null_pct` — against what you know about the data.
  The spec JSON is the contract; edit it freely before generating.
- The generated data reproduces *performance shape* (volumes, skew, join
  rates), not statistical content. Don't use it for result-correctness tests.
