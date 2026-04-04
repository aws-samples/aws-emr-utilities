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
│              │         │  │ emr_recommender.py│─▶│write_to_iceberg.py│          │
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
| `emr_recommender.py` | Generates cost and performance optimized Spark configurations |
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
python3 emr_recommender.py \
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

### emr_recommender.py

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

## Important Considerations

- **Use completed run event logs.** If a Spark application fails mid-execution, the event log will contain only partial workload data — shuffle volumes, task counts, and memory utilization will be underreported. Recommendations generated from failed runs may significantly undersize the configuration. The advisor automatically detects incomplete applications (SQL executions still running at termination) and adds a warning to the output, but for reliable sizing always use the event log from a successful completed run.

- **Recommendations are sized for the observed data volume.** The advisor sizes executors, shuffle partitions, and disk based on the input and shuffle volumes in the event log. If the actual data volume changes significantly between runs (e.g., a daily job processes 10x more data on month-end), the recommendation may undersize the configuration. Regenerate recommendations whenever the expected data volume changes materially.

## Prerequisites

- Python 3.7+
- `pip install boto3 zstandard pandas`
- For Spark-based extraction: EMR cluster or EMR Serverless application
- For Iceberg writes: Glue Catalog access, Iceberg Spark runtime JAR

## Legacy Scripts

Previous extraction scripts are in the `legacy/` folder. Both `spark_extractor.py` (PySpark) and `python_extractor.py` (pure Python) replace `legacy/spark_processor.py` with identical output format.

## License

MIT-0 License. See the LICENSE file.
