# EMR Serverless Config Advisor Pipeline

Automated pipeline for analyzing Spark event logs and generating EMR Serverless configuration recommendations. Processes logs in batches, extracts metrics, and provides cost and performance optimization recommendations.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         S3 Event Logs                           │
│                    (Spark History Server)                       │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ 01_discovery_job.py                                             │
│ • Queries Iceberg backlog table for unprocessed logs           │
│ • Filters by lookback window (last 1 hour)                     │
│ • Returns list of S3 paths to process                          │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ 02_orchestrator_emr_serverless.py (Runs on EMR Cluster)        │
│ • Batches logs into groups (5 logs per job)                    │
│ • Calculates batch size: SMALL (<1GB) or LARGE (≥1GB)          │
│ • Assigns executors: SMALL=3, LARGE=10                         │
│ • Capacity management: waits when cluster is full              │
│ • Submits EMR Serverless jobs in parallel                      │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼ (Multiple parallel jobs)
┌─────────────────────────────────────────────────────────────────┐
│              EMR Serverless Jobs (Concurrent)                   │
│  ┌──────────────────────────────────────────────────┐           │
│  │ 04_spark_extractor.py                            │           │
│  │ • Reads Spark event logs from S3                 │           │
│  │ • Extracts task/stage metrics                    │           │
│  │ • Extracts Spark configuration                   │           │
│  │ • Outputs: task_stage_summary + spark_config     │           │
│  └────────────────────┬─────────────────────────────┘           │
│                       ▼                                         │
│  ┌──────────────────────────────────────────────────┐           │
│  │ 06_emr_recommender.py                            │           │
│  │ • Analyzes extracted metrics                     │           │
│  │ • Generates cost optimization recommendations    │           │
│  │ • Generates performance recommendations          │           │
│  │ • Outputs: recommendations JSON                  │           │
│  └────────────────────┬─────────────────────────────┘           │
│                       ▼                                         │
│  ┌──────────────────────────────────────────────────┐           │
│  │ 07_write_to_s3_partitioned.py                    │           │
│  │ • Writes JSON to S3 with date-hour partitions    │           │
│  │ • Path: s3://.../datehour=YYYYMMDDHH/            │           │
│  └──────────────────────────────────────────────────┘           │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 25_bulk_load_metrics_to_iceberg.py                              │
│ • Discovers JSON files in S3 (last N hours)                    │
│ • Multi-threaded read: 40 parallel workers                     │
│ • Validates and flattens JSON records                          │
│ • Batched Iceberg writes: 500 records per batch                │
│ • Writes to: task_stage_summary + spark_config_extract tables  │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ 22_load_advisor_to_iceberg_dynamodb.py                          │
│ • Loads recommendations from S3 to DynamoDB                     │
│ • Enables fast lookups for downstream applications             │
│ • Handles 2000+ concurrent writes                              │
└─────────────────────────────────────────────────────────────────┘
```

## Script Execution Flow

### 1. Discovery Phase
**Script:** `01_discovery_job.py`

Discovers unprocessed event logs from Iceberg backlog table.

```bash
python3 01_discovery_job.py \
    --table "${CATALOG_NAMESPACE}.backlog_events_log" \
    --lookback-hours 1 \
    --limit 0
```

**Output:** List of S3 event log paths to process

---

### 2. Orchestration & Job Submission
**Script:** `02_orchestrator_emr_serverless.py` *(Runs on dedicated EMR Cluster)*

Batches logs and submits EMR Serverless jobs with dynamic resource allocation.

```bash
spark-submit \
    --packages org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.6.0 \
    --conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions \
    02_orchestrator_emr_serverless.py
```

**Key Features:**
- **Batching:** Groups 5 event logs per job (configurable)
- **Dynamic Scaling:**
  - SMALL batches (< 1 GB): 3 max executors → 10 vCPU/job
  - LARGE batches (≥ 1 GB): 10 max executors → 24 vCPU/job
- **Capacity Management:** Monitors cluster capacity, waits when full
- **Throttling:** API rate limiting to prevent rejections

**Environment Variables:**
```bash
EMR_SERVERLESS_APPLICATION_ID    # EMR app ID
BACKLOG_TABLE                    # Iceberg backlog table
MAX_CONCURRENT_JOBS              # Max jobs to submit (e.g., 500)
CAPACITY_MAX_CONCURRENT          # Max running jobs (e.g., 500)
```

---

### 3. Metrics Extraction (EMR Serverless)
**Script:** `04_spark_extractor.py` (runs on EMR Serverless)

Extracts metrics from Spark event logs.

**Outputs:**
- `task_stage_summary/{app_id}.json` - Task and stage metrics
- `spark_config_extract/{app_id}.json` - Spark configuration

**Key Metrics:**
- Task duration, executor CPU time, shuffle bytes
- Stage metrics, DAG info
- Spark properties and Hadoop configuration
- Job ID extraction (checks 3 nested JSON locations)

---

### 4. Recommendation Generation (EMR Serverless)
**Script:** `06_emr_recommender.py` (runs on EMR Serverless)

Analyzes metrics and generates recommendations.

**Outputs:**
- Cost optimization recommendations
- Performance optimization recommendations
- Resource configuration suggestions

---

### 5. S3 Partitioned Write (EMR Serverless)
**Script:** `07_write_to_s3_partitioned.py` (runs on EMR Serverless)

Writes results to S3 with date-hour partitioning.

**Output Structure:**
```
s3://${S3_BUCKET}/emr-serverless-config-advisor/
├── task-stage-summary/datehour=2024052212/
├── spark-config-extract/datehour=2024052212/
└── recommendations/datehour=2024052212/
```

---

### 6. Bulk Load to Iceberg
**Script:** `25_bulk_load_metrics_to_iceberg.py`

High-performance loading from S3 JSON to Iceberg tables.

```bash
python3 25_bulk_load_metrics_to_iceberg.py \
    --s3-bucket ${S3_BUCKET} \
    --lookback-hours 2
```

**Performance:**
- 40 parallel S3 readers (optimized for 64-core servers)
- 500 records per Iceberg write batch
- Processes 4000+ files in minutes

**Target Tables:**
- `task_stage_summary` - Task/stage execution metrics
- `spark_config_extract` - Spark configuration data

---

### 7. DynamoDB Integration
**Script:** `22_load_advisor_to_iceberg_dynamodb.py`

Loads recommendations to DynamoDB for fast lookups.

```bash
python3 22_load_advisor_to_iceberg_dynamodb.py \
    --s3-bucket ${S3_BUCKET} \
    --lookback-hours 2 \
    --dynamodb-table ${DYNAMODB_TABLE_NAME}
```

**Benefits:**
- Sub-millisecond lookups for downstream apps
- Handles 2000+ concurrent writes
- Supports cost and performance recommendations

---

## Pipeline Runners

### Hourly Pipeline (Production)
**Script:** `RUN_HOURLY_PIPELINE.sh`

Processes logs from the last 1 hour. Runs all steps sequentially.

```bash
./RUN_HOURLY_PIPELINE.sh
```

**Workflow:**
1. Discovery (01_discovery_job.py)
2. Orchestration (02_orchestrator_emr_serverless.py)
3. Wait for jobs to complete
4. Bulk load metrics (25_bulk_load_metrics_to_iceberg.py)
5. Load to DynamoDB (22_load_advisor_to_iceberg_dynamodb.py)

**Logging:** Uploads execution logs to S3

---

### Backfill Pipeline (Historical Data)
**Script:** `RUN_BACKFILL_PIPELINE.sh`

For processing large backlogs (days/weeks of historical data).

```bash
./RUN_BACKFILL_PIPELINE.sh
```

**Configuration:**
- Higher job limits (1500+ jobs)
- Separate EMR cluster (optional)
- Extended lookback window (96+ hours)

---

### Manual Single Job
**Script:** `03_run_single_emr_job.py`

Process specific event logs manually.

```bash
python3 03_run_single_emr_job.py \
    --application-id ${EMR_SERVERLESS_APPLICATION_ID} \
    --event-log-paths "s3://bucket/path/log1,s3://bucket/path/log2" \
    --output-path "s3://${S3_BUCKET}/output/" \
    --limit 100
```

---

## Table Maintenance

### Iceberg Table Compaction
**Script:** `30_compact_all_iceberg_tables.py`

Compacts Iceberg tables to optimize query performance.

```bash
python3 30_compact_all_iceberg_tables.py
```

**When to Run:**
- Weekly or when file count > 1000
- After large backfill operations
- When queries become slow

**Automated Setup:**
```bash
./33_setup_compaction_cron.sh  # Sets up daily cron job
./34_check_compaction_status.sh  # Check compaction status
```

---

## Setup & Configuration

### Prerequisites

- **AWS Resources:**
  - EMR Serverless Application (Spark 3.5+, Python 3.11+)
  - S3 bucket for event logs and outputs
  - Iceberg tables (via Glue Catalog or Hive Metastore)
  - DynamoDB table for recommendations
  - IAM role with S3/EMR/Glue/DynamoDB permissions

- **EMR Cluster:** For running orchestrator (needs Spark + PySpark)

### Environment Configuration

Copy and configure the template:

```bash
cp .env.template .env
nano .env
```

**Required Variables:**
```bash
AWS_ACCOUNT_ID=123456789012
AWS_REGION=us-east-1
S3_BUCKET=spark-history-${AWS_ACCOUNT_ID}-us-east-1
EMR_SERVERLESS_APPLICATION_ID=00gxxxxxxxxx
IAM_EXECUTION_ROLE_ARN=arn:aws:iam::${AWS_ACCOUNT_ID}:role/RoleName
CATALOG_NAMESPACE=database.schema
DYNAMODB_TABLE_NAME=emr-config-advisor
```

### Installation

#### 1. Create Python Virtual Environment with Dependencies

EMR Serverless jobs require a packaged Python virtual environment with all necessary dependencies.

**Step 1: Create virtual environment**
```bash
# Create a new virtual environment
python3 -m venv pyspark_venv

# Activate the virtual environment
source pyspark_venv/bin/activate
```

**Step 2: Install required dependencies**
```bash
# Core dependencies for pipeline
pip install --upgrade pip setuptools wheel

# AWS and data processing libraries
pip install boto3>=1.26.0
pip install pyiceberg[hive,glue]>=0.5.0

# Additional dependencies (if needed)
pip install pandas>=2.0.0
pip install pyarrow>=12.0.0

# Verify installations
pip list
```

**Step 3: Package virtual environment for EMR Serverless**
```bash
# Deactivate virtual environment
deactivate

# Package the entire virtual environment
cd pyspark_venv
tar -czf ../pyspark_venv.tar.gz .
cd ..

# Verify package size (should be < 1GB)
ls -lh pyspark_venv.tar.gz
```

**Step 4: Upload to S3**
```bash
# Create dependencies directory in S3
aws s3 mb s3://${S3_BUCKET}/pipeline-files/dependencies/ 2>/dev/null || true

# Upload packaged virtual environment
aws s3 cp pyspark_venv.tar.gz \
    s3://${S3_BUCKET}/pipeline-files/dependencies/pyspark_venv.tar.gz

# Verify upload
aws s3 ls s3://${S3_BUCKET}/pipeline-files/dependencies/
```

**Troubleshooting:**
- If package is too large (>1GB), exclude unnecessary files:
  ```bash
  tar -czf pyspark_venv.tar.gz \
      --exclude='*.pyc' \
      --exclude='__pycache__' \
      --exclude='*.dist-info' \
      .
  ```
- For Apple Silicon (M1/M2) Macs, use `--platform linux/amd64`:
  ```bash
  docker run --platform linux/amd64 -v $(pwd):/work -w /work python:3.11 bash -c \
      "python3 -m venv pyspark_venv && \
       source pyspark_venv/bin/activate && \
       pip install boto3 pyiceberg[hive,glue] && \
       cd pyspark_venv && tar -czf ../pyspark_venv.tar.gz ."
  ```

---

#### 2. Upload Pipeline Scripts to S3

```bash
# Upload all Python and Shell scripts
aws s3 cp . s3://${S3_BUCKET}/pipeline-files/ \
    --recursive --exclude "*" --include "*.py" --include "*.sh"

# Verify upload
aws s3 ls s3://${S3_BUCKET}/pipeline-files/
```

---

#### 3. Initialize Iceberg Tables

Create these tables in your Spark environment:

```sql
-- Backlog tracking table
CREATE TABLE ${CATALOG_NAMESPACE}.backlog_events_log (
    event_log_path STRING,
    status STRING,
    discovered_at TIMESTAMP,
    processed_at TIMESTAMP,
    batch_id STRING,
    job_run_id STRING
) USING iceberg
PARTITIONED BY (days(discovered_at));

-- Task/stage metrics table
CREATE TABLE ${CATALOG_NAMESPACE}.task_stage_summary (
    job_id STRING,
    application_id STRING,
    task_metrics STRUCT<...>,
    stage_metrics STRUCT<...>,
    timestamp TIMESTAMP
) USING iceberg;

-- Spark config table
CREATE TABLE ${CATALOG_NAMESPACE}.spark_config_extract (
    job_id STRING,
    application_id STRING,
    spark_properties MAP<STRING, STRING>,
    hadoop_properties MAP<STRING, STRING>,
    timestamp TIMESTAMP
) USING iceberg;
```

### Running the Pipeline

**First Run (Test):**
```bash
# Test with limited logs
export TEST_LIMIT=10
export LOOKBACK_HOURS=1
./RUN_HOURLY_PIPELINE.sh
```

**Production (Hourly):**
```bash
# Set up cron for hourly runs
0 * * * * /path/to/RUN_HOURLY_PIPELINE.sh
```

**Backfill (Historical):**
```bash
# Process last 4 days
export LOOKBACK_HOURS=96
export MAX_JOBS_PER_RUN=1500
./RUN_BACKFILL_PIPELINE.sh
```

---

## Monitoring & Troubleshooting

### Check Job Status
```bash
# List recent EMR jobs
aws emr-serverless list-job-runs \
    --application-id ${EMR_SERVERLESS_APPLICATION_ID} \
    --max-results 20

# Get job details
aws emr-serverless get-job-run \
    --application-id ${EMR_SERVERLESS_APPLICATION_ID} \
    --job-run-id <job-run-id>
```

### View Logs
```bash
# Pipeline execution logs (uploaded to S3)
aws s3 ls s3://${S3_BUCKET}/pipeline-files/hourly_pipeline_logs/
```

### Check Backlog Status
```sql
-- In Spark SQL or Athena
SELECT status, COUNT(*) 
FROM ${CATALOG_NAMESPACE}.backlog_events_log 
GROUP BY status;
```

### Common Issues

**CPU Capacity Errors:**
- Reduce `CAPACITY_MAX_CONCURRENT`
- Increase cluster capacity
- Use separate backfill cluster

**Slow Performance:**
- Increase `MAX_WORKERS` (40 recommended for 64 cores)
- Run table compaction
- Check network bandwidth

**Job Failures:**
- Check CloudWatch logs
- Verify IAM permissions
- Validate S3 paths

---

## Performance Tuning

### Batch Size (Orchestrator)
```python
# In 02_orchestrator_emr_serverless.py
LOGS_PER_JOB = 5  # Increase for faster processing (trade-off: larger jobs)
```

### Bulk Loading (25_bulk_load_metrics_to_iceberg.py)
```python
MAX_WORKERS = 40   # Match server core count
BATCH_SIZE = 500   # Records per Iceberg write
```

### Capacity Management
```bash
CAPACITY_MAX_CONCURRENT=500  # Based on cluster vCPU (vCPU / 10)
CAPACITY_CHECK_INTERVAL=300  # Check every 5 minutes when full
```

---

## Key Features

✅ **Batch Processing** - Process 5 logs per job (reduces overhead)  
✅ **Dynamic Scaling** - Auto-adjust executors based on batch size  
✅ **Capacity-Aware** - Prevents cluster overload and job rejections  
✅ **High-Speed Loading** - 40 parallel threads + batched writes  
✅ **Dual Storage** - Iceberg for analytics + DynamoDB for fast lookups  
✅ **Robust Job ID Extraction** - Checks multiple JSON locations  
✅ **Automated Maintenance** - Built-in table compaction  

---

## Contributing

1. Fork the repository
2. Create feature branch: `git checkout -b feature/new-feature`
3. Commit changes: `git commit -am 'Add new feature'`
4. Push to branch: `git push origin feature/new-feature`
5. Submit pull request

---

## License

Apache 2.0 License - See LICENSE file for details

---

## Support

For issues or questions:
- Check CloudWatch logs for EMR Serverless jobs
- Review pipeline logs in S3
- Query Iceberg tables for processing status
- Verify environment configuration in `.env`
