# EMR Serverless Advisor — Web UI

FastAPI web UI for the Config Advisor with an EMR-console-style shell.

## Features
- **Config Advisor**: upload raw Spark event logs (EMR Serverless or EMR on
  EC2; plain / .gz / .zstd / rolling `events_N` files, or a .zip of the
  `eventlog_v2_*` directory) or pre-extracted `task_stage_summary` JSONs.
  Server-side extraction, bottleneck classification, cost/performance worker
  recommendations, run cost (billed via the EMR API when the run is in the
  same account, estimated from executor lifetimes otherwise), query plans
  (Spark-UI-SQL-tab-style operator trees with metrics and per-operator
  failure attribution), and a deterministic recommended-vs-submitted config
  audit.
  Optionally attach a .zip of driver/executor stderr (driver log plus the
  stderr of any failed executors — EMR Serverless `SPARK_DRIVER`/
  `SPARK_EXECUTOR` and YARN `container_*` layouts are auto-detected): a
  curated signature scan adds the *why* the event log can't show — heap OOM
  vs container RSS kill vs disk full vs S3 throttling vs execution-memory
  contention — with excerpts, and corroboration notes on the bottleneck
  verdict.
- **EC2 → Serverless migration**: EC2 event logs are auto-detected and get
  migration-translated configs; upload the EC2 baseline and a Serverless
  attempt together for a migration-mode comparison.
- **Compare two runs**: metric deltas, noise-filtered config diff, per-stage
  CPU-per-GB analysis with an automatic verdict.
- **Observability**: live driver/executor dashboards (heap, RSS, CPU, GC,
  disk, tasks, job status) from a self-hosted Prometheus (see
  `bucket-agent/docs/METRICS_OBSERVABILITY_STACK.md` for the stack).
- **Ask AI**: embedded agentic assistant (Bedrock Converse tool-use) with
  tools over the EMR Serverless API, Prometheus, the analyses, and a
  t-shirt-sizing tool for brand-new jobs.

## Run
```bash
pip install fastapi 'uvicorn[standard]' python-multipart jinja2 boto3
export PROMETHEUS_URL=http://your-prometheus:9090   # optional, for Observability
export MODEL_ID=global.anthropic.claude-sonnet-4-6  # optional, for Ask AI
python3 app.py                                       # http://localhost:5000
```
Or `MONITORING_INSTANCE=i-xxxx ./start_advisor.sh` to run with a self-healing
SSM port-forward tunnel to a Prometheus host that isn't directly reachable.

This tool vendors no third-party code. Dependencies (FastAPI, uvicorn,
Jinja2, boto3) are installed via pip; Chart.js is loaded from CDN at runtime;
the optional Observability backend connects to a user-operated Prometheus
over HTTP. Consult each project's license for your own compliance needs.
