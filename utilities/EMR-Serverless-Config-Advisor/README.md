# EMR Serverless Config Advisor

Analyzes Spark event logs from EMR on EC2 or EMR Serverless and generates optimized EMR Serverless configurations. Produces cost-optimized and performance-optimized recommendations based on actual workload properties — input size, shuffle volume, memory utilization, disk spill, and shuffle I/O patterns.

## How It Works

The advisor extracts 80+ metrics from Spark event logs and uses them to calculate right-sized EMR Serverless configurations:

1. **Extract** — Parse Spark event logs (compressed `.zst` or plain JSON) to produce per-application metrics: task counts, stage-level shuffle/spill, executor memory and CPU utilization, I/O timing breakdowns, and driver statistics.

2. **Recommend** — Analyze extracted metrics to determine optimal worker size (Small/Medium/Large), executor count, shuffle partitions, disk configuration, and timeout settings. Two modes:
   - **Cost-optimized**: Minimum resources to complete the job reliably
   - **Performance-optimized**: Additional headroom for SLA-critical workloads

3. **Format** — Convert recommendations into deployment-ready EMR Serverless `sparkSubmitParameters` JSON.

### Key Sizing Decisions

| Decision | Based On |
|----------|----------|
| Worker type (Small 4c / Medium 8c / Large 16c) | Peak memory per executor, memory utilization |
| Executor memory | 1.5× observed peak memory, rounded to valid EMR Serverless increments |
| Max executors | Shuffle volume ÷ target partition size (1 GB default) ÷ cores per worker |
| Shuffle partitions | Total shuffle data ÷ 1 GB target partition size |
| Driver sizing | Partition count, executor count, and shuffle volume thresholds |
| Disk configuration | 500G shuffle-optimized attached disk (default) |
| Timeout tuning | Input size + duration-based network and shuffle timeouts |
| IO-aware scaling | For shuffle-bound jobs (>50% fetch wait), automatically switches to smaller workers with more disks to increase aggregate disk throughput |

## Architecture

```
┌──────────────┐         ┌──────────────────────────────────────────────────────┐
│   User /CI   │         │                    AWS Cloud                         │
│              │         │                                                      │
│  Invoke      │────────▶│  ┌─────────────────────┐                             │
│  Lambda      │         │  │  Lambda Orchestrator │                             │
│              │         │  │  (lambda_orchestrator │                             │
│              │         │  │   .py)                │                             │
│              │         │  └────────┬─────────────┘                             │
│              │         │           │ Submits 1 job per app (parallel)          │
│              │         │           ▼                                           │
│              │         │  ┌─────────────────────────────────────┐              │
│              │         │  │       EMR Serverless Application     │              │
│              │         │  │                                      │              │
│              │         │  │  ┌──────────────┐ ┌──────────────┐  │              │
│              │         │  │  │spark_extractor│ │spark_extractor│  │              │
│              │         │  │  │  (App 1)     │ │  (App 2)     │  │              │
│              │         │  │  └──────┬───────┘ └──────┬───────┘  │              │
│              │         │  │         │    ...N jobs    │          │              │
│              │         │  └─────────┼────────────────┼──────────┘              │
│              │         │            ▼                ▼                         │
│              │         │  ┌──────────────────────────────────┐                 │
│              │         │  │            Amazon S3              │                 │
│              │         │  │                                   │                 │
│              │         │  │  /event-logs/        (input)      │                 │
│              │         │  │  /task_stage_summary/ (extract)   │                 │
│              │         │  │  /spark_config/       (configs)   │                 │
│              │         │  │  /iceberg/            (table)     │                 │
│              │         │  └──────────────────────────────────┘                 │
│              │         │            │                                          │
│              │         │            ▼                                          │
│              │         │  ┌──────────────────┐  ┌───────────────────┐          │
│              │         │  │ emr_s_fine_tuner.py│─▶│write_to_iceberg.py│          │
│              │         │  │ (cost + perf)     │  │ (Spark + Glue)    │          │
│              │         │  └──────────────────┘  └───────────────────┘          │
│              │         │                                                      │
└──────────────┘         └──────────────────────────────────────────────────────┘
```

## Scripts

| Script | Purpose |
|--------|---------|
| `spark_extractor.py` | Extracts metrics from Spark event logs using PySpark (runs on EMR) |
| `python_extractor.py` | Extracts metrics from Spark event logs using pure Python (runs anywhere) |
| `pipeline_wrapper.py` | End-to-end orchestrator: extract → recommend → format (no Spark required) |
| `emr_s_fine_tuner.py` | Generates cost and performance optimized Spark configurations |
| `format_to_job_config.py` | Converts recommendations into EMR Serverless `sparkSubmitParameters` format |
| `lambda_orchestrator.py` | Lambda function that submits parallel EMR Serverless extraction jobs |
| `write_to_iceberg.py` | Writes metrics and recommendations to an Iceberg table via Spark |

## Quick Start

### Option 1: Pure Python Pipeline (no Spark required)

Run all three stages (extract → recommend → format) in one command on any machine with Python 3.7+:

```bash
python3 pipeline_wrapper.py \
  --input s3://your-bucket/event-logs/ \
  --output /tmp/advisor-output/ \
  --format-job-config
```

This produces:
- `task_stage_summary/*.json` — extracted metrics per application
- `recommendations.json` — cost and performance recommendations
- `job_config_*.json` — deployment-ready EMR Serverless configs

To re-run recommendations on previously extracted data:

```bash
python3 pipeline_wrapper.py \
  --input s3://your-bucket/event-logs/ \
  --output /tmp/advisor-output/ \
  --skip-extraction \
  --format-job-config
```

### Option 2: Step-by-Step

Extract metrics:

```bash
python3 python_extractor.py \
  --input s3://your-bucket/event-logs/ \
  --output /tmp/extracted/
```

Generate recommendations:

```bash
python3 emr_s_fine_tuner.py \
  --input-path /tmp/extracted/ \
  --output-cost cost.json \
  --output-perf perf.json
```

Format for deployment:

```bash
python3 format_to_job_config.py --input cost.json --output job_config.json
```

### Option 3: Lambda + EMR Serverless (at scale)

For processing hundreds of applications, deploy `lambda_orchestrator.py` as a Lambda function:

```bash
aws lambda invoke \
  --function-name your-lambda-function \
  --payload '{
    "input_path": "s3://your-bucket/event-logs/",
    "output_path": "s3://your-bucket/advisor-output/",
    "application_id": "YOUR_EMR_SERVERLESS_APP_ID",
    "execution_role": "arn:aws:iam::ACCOUNT:role/YourRole",
    "script_path": "s3://your-bucket/scripts/spark_extractor.py",
    "archives_path": "s3://your-bucket/scripts/zstandard.zip"
  }' \
  --cli-read-timeout 910 \
  output.json
```

### Option 4: Direct spark-submit

```bash
spark-submit --master local[*] --driver-memory 32g \
  spark_extractor.py \
  --input s3://your-bucket/event-logs/ \
  --output /tmp/output/
```

## Optimizing Serverless Configurations: A Two-Phase Approach

Use a two-phase process to ensure optimal performance and cost for your EMR Serverless deployments.

### Phase 1: Initial Serverless Configuration (Quick Start)

We start by estimating the necessary serverless resources (worker size, number of executors, disk space) based on metrics from your existing EC2 environment. This initial configuration is intentionally conservative — slightly over-provisioned to guarantee your job completes successfully on the first attempt.

**Goal:** Ensure a successful first run, even if it's not the most cost-effective.

### Phase 2: Optimal Serverless Configuration (Fine-Tuning)

This phase leverages actual runtime data from your serverless job — things like how much data was spilled to disk, fetch wait times, and actual disk I/O. We eliminate guesswork and rely on real-world performance data ("ground truth").

**Goal:** Achieve the best possible cost and performance.

### Why This Two-Phase Approach Is Effective

EC2 metrics are helpful for an initial estimate, but serverless environments behave differently. Here's a breakdown of key differences:

| Factor | Phase 1 (EC2 Source) | Phase 2 (Serverless Source) |
|--------|---------------------|---------------------------|
| **Spill** | Must predict (EC2 memory config ≠ Serverless) | Measures actual spill |
| **Shuffle** | Same data, but different configs change AQE coalescing | Measures actual distribution |
| **Broadcast** | Can't predict HashedRelation expansion | Measures actual broadcast sizes |
| **Fetch Wait** | EC2 uses shared local NVMe; Serverless uses per-executor disk | Measures actual contention |
| **Executor Utilization** | Estimated from EC2 utilization | Measures actual idle/active time |
| **Disk Throughput** | Predicted from specs | Identifies actual bottleneck |

**Spill:** Predicting spill (when data exceeds memory) is difficult when moving from EC2 to Serverless due to differences in memory-per-core ratios. Phase 2 measures actual spill.

**Shuffle:** Although the underlying data and Spark engine are consistent, the configurations we recommend for Serverless (executor count, partition count, advisory size) differ from EC2. These differences alter how Adaptive Query Execution (AQE) optimizes partition coalescing and shuffle data distribution. Phase 2 precisely measures actual shuffle volume under the Serverless configuration.

**Broadcast:** Predicting the final size of broadcasted data is difficult due to dynamic data sizes and the potential expansion of HashedRelation tables (Parquet on disk → deserialized in-memory hash table can be 5-10× larger). Phase 2 measures actual broadcast sizes using accumulator metrics.

**Fetch Wait:** Shuffle read happens over the network between executors in both EC2 and Serverless, and fetch wait time is recorded in both event logs. However, the disk architecture differs significantly: on EC2, multiple executors share fast local NVMe on the same node, while on Serverless, each executor gets its own dedicated disk with a fixed bandwidth cap (250 MiB/s for shuffle-optimized). Fetch wait tends to be higher on Serverless for disk-heavy jobs.

**In short:** Phase 1 leverages existing EC2 metrics to create an initial EMR Serverless configuration. Phase 2 refines this with precise, data-driven insights from the EMR Serverless environment.

### How It Works

The recommendation engine supports both phases (controlled by an `is_ec2` flag derived from the event log format). The workflow is:

1. **Run Phase 1** — Analyze EC2 event logs to produce an initial Serverless configuration
2. **Run the job** on EMR Serverless with the Phase 1 config
3. **Run Phase 2** — Analyze the Serverless event log to produce an optimized configuration

```bash
# Phase 1: from EC2 source
python3 pipeline_wrapper.py --input s3://bucket/ec2-event-logs/ --output /tmp/phase1/

# Phase 2: from Serverless run
python3 pipeline_wrapper.py --input s3://bucket/serverless-event-logs/ --output /tmp/phase2/
```

## Future State (Preview)

### Feedback-Driven Optimization with GenAI

The Config Advisor evolves from a one-shot recommender into a continuously learning system that combines deterministic rules with GenAI-powered diagnostics.

#### Architecture

```
┌──────────┐     ┌──────────────┐     ┌──────────────┐     ┌────────────────┐
│ Job Run  │────>│  Extractor   │────>│  Feedback    │────>│  Iceberg Table │
│ (Event   │     │  (enhanced)  │     │  Engine      │     │  (full history)│
│   Log)   │     │              │     │  (rules)     │     │                │
└──────────┘     └──────────────┘     └──────────────┘     └───────┬────────┘
                                                                    │
       ┌────────────────────────────────────────────────────────────┘
       │
       ▼
  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
  │  GenAI       │────>│ Recommender  │────>│  Next Run    │
  │  (periodic)  │     │ (constraints │     │  Config      │
  │              │     │  + formulas) │     │              │
  └──────────────┘     └──────────────┘     └──────────────┘
```

#### Capabilities

| Capability | Rule-Based | GenAI-Assisted |
|-----------|-----------|----------------|
| **Partition sizing** (shuffle / per-task memory) | ✅ | |
| **Executor count** (disk IO / throughput) | ✅ | |
| **Worker type selection** (shuffle volume thresholds) | ✅ | |
| **Disk sizing** (per-executor shuffle + spill + headroom) | ✅ | |
| **Spill elimination** (detect advisory too large) | ✅ | |
| **Compression recommendation** (CPU idle + high shuffle) | ✅ | |
| **IO-downsize guard** (spill indicates capacity, not IOPS) | ✅ | |
| **Failure diagnosis from driver logs** (exact root cause) | | ✅ |
| **Trend prediction** (data growth, seasonality) | | ✅ |
| **Cross-job correlation** (shared tables growing) | | ✅ |
| **Anomaly explanation** (why this run cost 4x more) | | ✅ |
| **SQL optimization suggestions** (UNION→UNION ALL, column pruning) | | ✅ |
| **Recommendation narrative** (human-readable explanation) | | ✅ |
| **Regression detection** (new config worse than previous) | ✅ | |
| **Convergence tracking** (stop tuning when stable) | ✅ | |

#### Feedback Loop

After each run, the system records outcomes and updates learned constraints:

| Signal | Rule | Constraint Updated |
|--------|------|-------------------|
| Spill > shuffle volume | Partitions too large | `min_partitions` increased |
| Fetch wait > 20% | Insufficient disk bandwidth | `min_executors` increased |
| FetchFailed tasks > 0 | Disk full or executor OOM | `min_disk` increased |
| No issues + fast completion | Over-provisioned | Executor floor relaxed by 10% |
| Broadcast crash (>8 GB) | HashedRelation too large | `max_broadcast_threshold` lowered |

#### GenAI Integration Points

1. **Post-run diagnosis** — Parse unstructured driver logs, identify novel errors, produce actionable explanation
2. **Weekly trend analysis** — Analyze full run history, detect seasonality and growth patterns, predict next run's needs
3. **Cross-job insights** — Correlate shared table growth across multiple jobs, proactively adjust affected configs
4. **Recommendation narrative** — Generate human-readable explanation of why each config value was chosen
5. **SQL plan analysis** — Identify query anti-patterns (redundant sorts, unnecessary UNION DISTINCT, missing column pruning)

#### Optional Log Enrichment

For richer diagnostics, the pipeline accepts optional log sources:

```bash
# Standard: event log only
python3 pipeline_wrapper.py --input s3://bucket/event-log.zip --output /tmp/out/

# Enriched: event log + driver log
python3 pipeline_wrapper.py --input s3://bucket/event-log.zip --output /tmp/out/ \
  --driver-log s3://bucket/driver/stderr.gz

# Full: event log + complete S3 log directory
python3 pipeline_wrapper.py --input s3://bucket/event-log.zip --output /tmp/out/ \
  --log-path s3://emr-logs/cluster-id/
```

| Source | Additional Signals |
|--------|-------------------|
| Event log only | Stage metrics, executor counts, shuffle/spill totals |
| + Driver log | Exact error messages, AQE coalescing decisions, retry counts, broadcast failures |
| + Full log path | Per-executor GC/heap, instance-state (disk util, memory), CloudWatch metrics |

#### Convergence

A job's config is considered converged when 3 consecutive runs show:
- Fetch wait < 10%
- Spill < 5% of shuffle volume
- Zero task/stage failures
- Cost within 5% of previous run

At convergence, the system monitors for drift without recommending further changes.

## Recommendation Modes

| Mode | Strategy | Best For |
|------|----------|----------|
| Cost | Minimum resources to complete reliably. For shuffle-bound jobs (>50% fetch wait), automatically uses smaller workers with more disks to increase I/O throughput without over-provisioning compute. | Dev/test, batch workloads, cost-sensitive production |
| Performance | Additional executor headroom (1.5–2× cost). For shuffle-bound jobs, scales the IO-aware worker configuration to the higher executor count. | SLA-critical production, latency-sensitive jobs |

### IO-Aware Scaling

When a job spends more than 50% of its time waiting on shuffle fetches, the standard Large-worker configuration won't help — the bottleneck is disk I/O, not compute. The advisor automatically detects this and:

- Switches to smaller workers (Medium 8c or Small 4c) to increase the number of independent disks
- Keeps the same total vCPU and per-task memory (e.g., Large 16c/108G → Small 4c/27G = same 6.75 GB/task)
- Limits the multiplier based on fleet size: jobs with >200 cost executors cap at 2× (Large→Medium) to avoid excessive shuffle network connections; smaller jobs allow up to 4× (Large→Small)

This is applied transparently to both cost and performance outputs — no separate flag needed.

## Extracted Metrics

Each application produces a JSON with these sections:

| Section | Key Fields |
|---------|------------|
| `task_summary` | Total/completed/failed/killed tasks, success rate |
| `stage_summary` | Per-stage shuffle read/write, spill, duration, failure reasons |
| `executor_summary` | Per-executor cores, memory, uptime, utilization, peak memory, cost factor |
| `io_summary` | Total input/output/shuffle bytes, shuffle fetch wait %, GC %, write time % |
| `spill_summary` | Memory and disk spill totals and percentages |
| `shuffle_data_summary` | Peak stage shuffle write, EMR Serverless storage eligibility |
| `driver_metrics` | GC stats, off-heap memory, tasks/jobs/stages launched |
| `job_details` | Per-job duration, status, stage mapping |
| `sql_metrics` | Per-SQL execution plan, duration, status |

## Serverless Storage

Serverless storage is **disabled by default**. Recommendations use attached executor disk (`spark.emr-serverless.executor.disk: 500G`).

Pass `--serverless-storage` to enable it. Serverless storage will only be recommended when:
- The workload has **zero disk spill**
- Shuffle volume per stage is within safe limits

This is because EMR Serverless local disk is fixed at 20GB and cannot be increased. Shuffle sort spill and memory spill overflow go to local disk, not serverless storage. Enabling serverless storage for spill-heavy jobs causes "No space left on device" failures.

## Write to Iceberg Table

Optionally persist recommendations to an Iceberg table for tracking over time:

```bash
spark-submit \
  --packages org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.6.1,software.amazon.awssdk:bundle:2.28.11,software.amazon.awssdk:url-connection-client:2.28.11 \
  write_to_iceberg.py \
  --rec-path s3://your-bucket/recommendations/cost.json \
  --extract-path s3://your-bucket/advisor-output/ \
  --table glue_catalog.your_database.config_advisor \
  --warehouse s3://your-bucket/iceberg/
```

The table can also be created manually via Athena (V3 engine) or Spark SQL:

```sql
CREATE TABLE IF NOT EXISTS your_database.config_advisor (
    job_id                          STRING,
    application_name                STRING,
    app_id                          STRING,
    optimization_mode               STRING,
    input_gb                        DOUBLE,
    shuffle_read_gb                 DOUBLE,
    shuffle_write_gb                DOUBLE,
    peak_shuffle_write_per_stage    DOUBLE,
    peak_disk_spill_per_stage       DOUBLE,
    duration_hours                  DOUBLE,
    duration_minutes                DOUBLE,
    avg_memory_utilization_percent  DOUBLE,
    avg_cpu_utilization_percent     DOUBLE,
    max_memory_utilization_percent  DOUBLE,
    idle_core_percentage            DOUBLE,
    total_memory_spilled_gb         DOUBLE,
    cost_factor                     DOUBLE,
    src_event_log_location          STRING,
    recommendation                  STRING,
    created_at                      STRING
)
USING iceberg
LOCATION 's3://your-bucket/iceberg/your_database/config_advisor/'
```

### Example Queries

```sql
-- Latest recommendations
SELECT job_id, application_name, optimization_mode, input_gb,
       duration_hours, cost_factor
FROM your_database.config_advisor
ORDER BY created_at DESC;

-- Jobs exceeding Serverless storage limit
SELECT job_id, application_name, peak_shuffle_write_per_stage
FROM your_database.config_advisor
WHERE peak_shuffle_write_per_stage > 200;

-- High memory utilization jobs
SELECT job_id, application_name, avg_memory_utilization_percent,
       max_memory_utilization_percent, total_memory_spilled_gb
FROM your_database.config_advisor
WHERE max_memory_utilization_percent > 85
ORDER BY total_memory_spilled_gb DESC;
```

## CLI Reference

### python_extractor.py

| Flag | Description | Default |
|------|-------------|---------|
| `--input` | S3 path or local path to event logs | *required* |
| `--output` | Output path for extracted metrics | *required* |
| `--limit` | Max applications to process | 100 |
| `--single-app` | Input path is a single app (not a directory of apps) | false |
| `--workers` | Parallel processing workers | 20 |
| `--profile` | AWS profile name for S3 access | default |

### emr_s_fine_tuner.py

| Flag | Description | Default |
|------|-------------|---------|
| `--input-path` | Path with extracted metrics (local or S3) | *required* |
| `--output-cost` | Output file for cost-optimized recommendations | `recommendations_cost_optimized.json` |
| `--output-perf` | Output file for performance-optimized recommendations | `recommendations_performance_optimized.json` |
| `--cost-optimized` | Generate only cost recommendations | both |
| `--performance-optimized` | Generate only performance recommendations | both |
| `--individual-files` | One JSON file per application | single file |
| `--format-job-config` | Output in deployment-ready format | standard |
| `--target-partition-size` | Target shuffle partition size in MiB | 1024 |
| `--limit` | Max applications to process | 100 |
| `--serverless-storage` | Enable serverless storage recommendations | off |

### pipeline_wrapper.py

| Flag | Description | Default |
|------|-------------|---------|
| `--input` | S3 or local path to event logs | *required* |
| `--output` | Output path for extracted metrics | *required* |
| `--limit` | Max applications to process | 100 |
| `--workers` | Parallel workers for extraction | 20 |
| `--profile` | AWS profile name | default |
| `--single-app` | Treat `--input` as a single app path | false |
| `--region` | AWS region | us-east-1 |
| `--target-partition-size` | Target shuffle partition size in MiB | 1024 |
| `--results` | Output filename for recommendations | recommendations.json |
| `--format-job-config` | Also produce EMR Serverless job config JSON | false |
| `--cost-optimized` | Generate only cost-optimized recommendations | both |
| `--performance-optimized` | Generate only performance-optimized recommendations | both |
| `--individual-files` | Generate individual JSON files per application | single file |
| `--write-to-iceberg-table` | Write to Iceberg table (`catalog.database.table`) | — |
| `--skip-extraction` | Skip extraction, use existing data in `--output` | false |
| `--serverless-storage` | Enable serverless storage recommendations | off |

### format_to_job_config.py

| Flag | Description | Default |
|------|-------------|---------|
| `--input` | Input recommendations JSON file | *required* |
| `--output` | Output job config JSON file | *required* |

### spark_extractor.py

| Flag | Description | Default |
|------|-------------|---------|
| `--input` | S3 path or local path to event logs | *required* |
| `--output` | Output path for extracted metrics | *required* |
| `--limit` | Max applications to process | 100 |
| `--single-app` | Input path is a single app | false |
| `--decompress-workers` | Parallel S3 download threads | 50 |

### write_to_iceberg.py

| Flag | Description | Default |
|------|-------------|---------|
| `--rec-path` | Path to recommendation JSON | *required* |
| `--extract-path` | Path containing `task_stage_summary/` | *required* |
| `--table` | Iceberg table: `catalog.database.table` | *required* |
| `--warehouse` | S3 warehouse location | — |

## Prerequisites

- Python 3.7+
- `pip install boto3 zstandard pandas`
- For Spark-based extraction: EMR cluster or EMR Serverless application
- For Iceberg writes: Glue Catalog access, Iceberg Spark runtime JAR

## Legacy Scripts

Previous extraction scripts are in the `legacy/` folder. Both `spark_extractor.py` (PySpark) and `python_extractor.py` (pure Python) replace `legacy/spark_processor.py` with identical output format.

## License

MIT-0 License. See the LICENSE file.

## TPC-DS Benchmark Results (3TB, EMR Serverless, emr-7.13.0)

Evaluated on TPC-DS at 3TB scale, 104 queries, EMR Serverless release emr-7.13.0 (us-east-1).

| Metric | Improvement |
|--------|-------------|
| **Runtime** | **-72.7%** |
| **Cost** | **-18.3%** |
| **Regressions** | 0 |



---

## Bucket Recommender (No Event Log Required)

For **new jobs without event log history**, the Bucket Recommender provides optimal starting configurations based on workload characteristics.

### Quick Start

```bash
# First run — just pick your size:
python3 emr_s_tshirt_size.py --size M

# With a sub-category if you know your workload pattern:
python3 emr_s_tshirt_size.py --size L --sub-category Shuffle-Optimized

# Output as spark-submit parameters (paste directly into StartJobRun):
python3 emr_s_tshirt_size.py --size L --format spark-submit

# After first successful run — use the full Config Advisor with the event log:
python3 emr_s_fine_tuner.py --input-path s3://your-bucket/event-logs/application_id/
```

### Two-Step Selection

**Step 1: Choose Size** (by input data volume):

| Size | Input Data | Duration | Default maxExecutors |
|------|-----------|----------|---------------------|
| XS | Near-zero (<5GB, <5min) | <5 min | 3 |
| S | 5–100 GB | 5–30 min | 50 |
| M | 100 GB – 1 TB | 15–60 min | 100 |
| L | 1–5 TB | 20–120 min | 200 |
| XL | >5 TB | 1–4 hours | 500 |

**Step 2: Choose Sub-Category** (default = General):

| Sub-Category | When to Use |
|---|---|
| **General** | Default. Safe for any workload. Pick this if unsure. |
| **Compute-Optimized** | Pure scan/filter/write, shuffle <10% of input |
| **Shuffle-Optimized** | Shuffle >1TB or >30% of input, GROUP BY, multi-table JOINs |
| **Memory-Optimized** | 20+ JOINs, wide tables (100+ cols), OOM history |
| **IO-Optimized** | Tiny input (<10GB) with 100x+ fan-out (EXPLODE, CROSS JOIN) |
| **Iceberg-Maintenance** | Compaction, expire snapshots, rewrite manifests |

### Two-Phase Optimization

| Phase | Tool | Input | When |
|-------|------|-------|------|
| **1. First run** | `emr_s_tshirt_size.py` | Size + sub-category | No event log yet |
| **2. Subsequent runs** | `emr_s_fine_tuner.py` | Event log S3 path | After first successful run |

> Start with the Bucket Recommender. After the job completes, its event log is written to S3. Feed that path to the full Config Advisor for optimal sizing on all subsequent runs.
| Event Log | + `--task-hours` from prior run | Best (same formula as full Config Advisor) |

### Sizing Guide

See [docs/sizing-guide.md](docs/sizing-guide.md) for the full selection guide with examples and decision flowchart.
