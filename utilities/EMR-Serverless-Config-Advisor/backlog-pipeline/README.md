# EMR Serverless Configuration Advisor - Backlog-Enabled Pipeline

Automated pipeline for generating Spark configuration recommendations for AWS EMR Serverless by analyzing historical Spark event logs.

## Overview

This pipeline uses a backlog table to track and process Spark event logs incrementally, generating optimized configurations for cost and performance.

**Key Features:**
- ✅ Backlog-based processing (no duplicate work)
- ✅ Incremental processing of large event logs
- ✅ Cost and performance optimization modes
- ✅ Iceberg table integration
- ✅ Configurable batch processing

## Architecture

The core pipeline (01-06) is self-contained — no external AI/ML dependencies.
The feedback layer (07-10) is opt-in for teams that want data-driven parameter tuning.

```
┌─────────────────────┐
│ 1. Discovery Job    │  Scans S3 for new event logs
│ (01_discovery_job)  │  Adds to backlog table (status='N')
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 2. Orchestrator     │  Pulls batch from backlog
│ (02_orchestrator)   │  Processes each log
└──────────┬──────────┘
           │
           ├──────────────────┬──────────────────┬──────────────────┐
           ▼                  ▼                  ▼                  ▼
    ┌──────────┐      ┌──────────┐      ┌──────────┐      ┌──────────┐
    │  Step 1  │      │  Step 2  │      │  Step 3  │      │  Step 4  │
    │ Extract  │ -->  │  Iceberg │ -->  │Recommend │ -->  │  Write   │
    │ Metrics  │      │  Load    │      │Generator │      │ to Table │
    └──────────┘      └──────────┘      └──────────┘      └──────────┘
                                              │
                                              ▼
┌────────────────── AI/ML Feedback Layer (OPT-IN) ─────────────────────────────┐
│                                                                               │
│  ┌──────────┐      ┌──────────┐      ┌──────────┐      ┌──────────┐         │
│  │  Step 5  │      │  Step 6  │      │  Step 7  │      │  Step 8  │         │
│  │Benchmark │ -->  │ Feedback │ -->  │  Model   │      │LLM Plan  │         │
│  │ Runner   │      │   Loop   │      │ Trainer  │      │ Analyzer │         │
│  └──────────┘      └──────────┘      └──────────┘      └──────────┘         │
│    (07)              (08)              (09)              (10)                  │
│  Runs workloads    Predicted vs     Learns params     Claude diagnoses       │
│  with rec configs  actual deltas    from feedback     anomalous plans        │
│                         │                │                                    │
│                         ▼                ▼                                    │
│                   ┌──────────────────────────┐                               │
│                   │   model-latest.json      │──> Recommender (Step 3)       │
│                   │   (learned parameters)   │                               │
│                   └──────────────────────────┘                               │
└───────────────────────────────────────────────────────────────────────────────┘
```

## Pipeline Scripts

| Script | Purpose | When to Run |
|--------|---------|-------------|
| `01_discovery_job.py` | Discover new event logs in S3 | Every 30 min - 1 hour |
| `02_orchestrator_backlog.py` | Process regular logs (<1GB) | Every 1-2 hours |
| `02_orchestrator_backlog-1gb_plus_events.py` | Process large logs (>=1GB) | Every 1-2 hours |
| `03_spark_extractor.py` | Extract metrics from event logs | Called by orchestrator |
| `04_json_to_iceberg_enhanced.py` | Load metrics to Iceberg tables | Called by orchestrator |
| `05_emr_recommender.py` | Generate recommendations | Called by orchestrator |
| `06_write_to_iceberg.py` | Write recommendations to table | Called by orchestrator |
| `07_benchmark_runner.py` | Run workloads with recommended configs | Opt-in: nightly / on-demand |
| `08_feedback_loop.py` | Compare predicted vs actual outcomes | Opt-in: after benchmark runs |
| `09_model_trainer.py` | Learn parameters from feedback data | Opt-in: weekly / after 20+ records |
| `10_llm_plan_analyzer.py` | LLM diagnosis of anomalous query plans | Opt-in: on-demand |

> **Note:** Scripts 07-10 are entirely optional. The core pipeline (01-06) works
> standalone with no AI/ML dependencies. The feedback layer is for teams that want
> to continuously improve recommendations by measuring actual outcomes.

## Quick Start (5 Minutes)

### 1. Configure Scripts

Edit **4 Python files** and set your environment-specific values.

#### File 1: `01_discovery_job.py` (Lines 40-75)

```python
# Line 40 - REQUIRED: Your S3 bucket
S3_BUCKET = "spark-history-YOUR-ACCOUNT-ID-YOUR-REGION"

# Line 44 - S3 prefix where event logs are stored
S3_PREFIX = "logs/"

# Line 49 - Backlog table name
BACKLOG_TABLE = "data_processing.backlog_events_log"
```

#### File 2: `02_orchestrator_backlog.py` (Lines 95-150)

```python
# Line 95 - REQUIRED: Your S3 bucket (same as discovery)
S3_BUCKET = "spark-history-YOUR-ACCOUNT-ID-YOUR-REGION"

# Line 100 - Where you uploaded these scripts
S3_SCRIPTS_PREFIX = "pipeline-files/backlog-enabled"

# Line 110 - Backlog table (must match discovery job)
BACKLOG_TABLE = "data_processing.backlog_events_log"

# Line 113 - Recommendations output table
ICEBERG_TABLE = "data_processing.serverless_config_advisor_v2"

# Line 119 - Batch size (500 for regular logs)
BATCH_SIZE = 500
```

#### File 3: `02_orchestrator_backlog-1gb_plus_events.py` (Lines 95-145)

Same as File 2, but with `BATCH_SIZE = 5` (line 118) for large files.

#### File 4: `04_json_to_iceberg_enhanced.py` (Lines 29-80)

```python
# Line 29 - REQUIRED: Your S3 bucket
INPUT_BUCKET = "spark-history-YOUR-ACCOUNT-ID-YOUR-REGION"

# Line 42 - REQUIRED: Your Hive Metastore URI
HMS_URI = "thrift://YOUR-HMS-HOST:9083"

# Line 51 - Database name
ICEBERG_NAMESPACE = "data_processing"
```

### 2. Create Backlog Table

Run this SQL in your Hive Metastore or Spark:

```sql
CREATE TABLE IF NOT EXISTS data_processing.backlog_events_log (
    event_log_id STRING COMMENT 'Unique ID (hash of S3 path)',
    application_id STRING COMMENT 'Spark application ID',
    s3_full_path STRING COMMENT 'Full S3 path to event log',
    s3_file_size BIGINT COMMENT 'File size in bytes',
    s3_last_modified TIMESTAMP COMMENT 'S3 last modified timestamp',
    is_processed STRING COMMENT 'Status: N=New, IP=InProgress, Y=Success, Y-F=Failed',
    job_id STRING COMMENT 'Extracted job ID',
    processing_instance_id STRING COMMENT 'Pipeline instance ID',
    processing_started_at TIMESTAMP COMMENT 'Processing start time',
    processing_completed_at TIMESTAMP COMMENT 'Processing completion time',
    processing_duration_seconds BIGINT COMMENT 'Duration in seconds',
    processing_attempt_count INT COMMENT 'Number of attempts',
    error_message STRING COMMENT 'Error message if failed',
    error_timestamp TIMESTAMP COMMENT 'Error timestamp',
    created_at TIMESTAMP COMMENT 'Record creation time',
    updated_at TIMESTAMP COMMENT 'Last update time'
)
USING iceberg
PARTITIONED BY (days(s3_last_modified))
COMMENT 'Tracks Spark event logs for backlog processing';
```

### 3. Upload Scripts to S3

```bash
# Set your S3 bucket
export S3_BUCKET="spark-history-123456789012-us-east-1"
export S3_PREFIX="pipeline-files/backlog-enabled"

# Upload all Python scripts
aws s3 cp . s3://${S3_BUCKET}/${S3_PREFIX}/ \
    --recursive \
    --exclude "*" \
    --include "*.py"

# Verify upload
aws s3 ls s3://${S3_BUCKET}/${S3_PREFIX}/
```

### 4. Run Discovery Job

```bash
spark-submit \
    --master yarn \
    --deploy-mode cluster \
    01_discovery_job.py
```

### 5. Run Orchestrator

```bash
# For regular-sized logs (<1GB)
spark-submit \
    --master yarn \
    --deploy-mode cluster \
    --conf spark.dynamicAllocation.maxExecutors=50 \
    02_orchestrator_backlog.py

# For large logs (>=1GB)
spark-submit \
    --master yarn \
    --deploy-mode cluster \
    --conf spark.dynamicAllocation.maxExecutors=20 \
    02_orchestrator_backlog-1gb_plus_events.py
```

## Configuration Reference

### Required Variables

| Variable | Location | Description | Example |
|----------|----------|-------------|---------|
| `S3_BUCKET` | All 4 scripts | S3 bucket with event logs | `spark-history-123456789012-us-east-1` |
| `HMS_URI` | 04_json_to_iceberg | Hive Metastore URI | `thrift://hms.example.com:9083` |

### Optional Variables (Have Defaults)

| Variable | Default | Description |
|----------|---------|-------------|
| `S3_PREFIX` | `logs/` | Event log location in S3 |
| `S3_SCRIPTS_PREFIX` | `pipeline-files/backlog-enabled` | Pipeline scripts location |
| `BACKLOG_TABLE` | `data_processing.backlog_events_log` | Backlog tracking table |
| `ICEBERG_TABLE` | `data_processing.serverless_config_advisor_v2` | Recommendations output table |
| `ICEBERG_NAMESPACE` | `data_processing` | Database/schema name |
| `BATCH_SIZE` | `500` (regular) / `5` (large) | Logs per run |
| `DISCOVERY_LOOKBACK_HOURS` | `1` | Discovery time window |
| `AWS_PROFILE` | `None` | AWS CLI profile |
| `AWS_REGION` | `us-east-1` | AWS region |

### Environment Variable Override

All variables can be overridden with environment variables:

```bash
export S3_BUCKET="my-bucket"
export BATCH_SIZE="1000"
export DISCOVERY_LOOKBACK_HOURS="2"

spark-submit 01_discovery_job.py
```

## Output

### Backlog Table Status Values

- `N` - New (not processed)
- `IP` - In Progress (claimed by orchestrator)
- `Y` - Successfully processed
- `Y-F` - Processed but failed

### Spark Configurations Generated

The recommender generates **24 Spark configurations** including:

**Resource Configuration:**
- Driver/Executor cores and memory
- Dynamic allocation settings (min/max executors)
- EMR Serverless disk configuration

**Query Optimization:**
- Adaptive query execution (AQE)
- Shuffle partitions (auto-calculated)
- File partition size

**Network & Timeouts:**
- Network timeout (600s-3600s)
- Shuffle connection timeout (600s-1800s)

**S3 Configuration:**
- S3A max attempts (15)

**Iceberg Configuration:**
- Catalog settings
- Extensions

**OpenLineage Configuration:**
- Parent run tracking
- Job namespace

**Conditional:**
- Shuffle compression (if shuffle > 30%)
- Serverless storage (if conditions met)

## Monitoring

### Check Backlog Status

```sql
SELECT is_processed, COUNT(*) as count
FROM data_processing.backlog_events_log
GROUP BY is_processed
ORDER BY is_processed;
```

### Check Processing Failures

```sql
SELECT event_log_id, application_id, error_message, error_timestamp
FROM data_processing.backlog_events_log
WHERE is_processed = 'Y-F'
ORDER BY error_timestamp DESC
LIMIT 10;
```

### View Recent Recommendations

```sql
SELECT job_id, application_name, optimization_mode,
       input_gb, duration_hours, created_at
FROM data_processing.serverless_config_advisor_v2
ORDER BY created_at DESC
LIMIT 10;
```

## Scheduling

### Option 1: Cron Jobs

```bash
# Discovery job - every 30 minutes
*/30 * * * * spark-submit 01_discovery_job.py

# Orchestrator - every 2 hours
0 */2 * * * spark-submit 02_orchestrator_backlog.py
```

### Option 2: AWS EventBridge

Create EventBridge rules to trigger EMR Serverless jobs on schedule.

### Option 3: Airflow

```python
from airflow import DAG
from airflow.providers.amazon.aws.operators.emr import EmrServerlessStartJobOperator
from datetime import datetime, timedelta

dag = DAG(
    'emr_config_advisor',
    schedule_interval='*/30 * * * *',
    start_date=datetime(2024, 1, 1)
)

discovery = EmrServerlessStartJobOperator(
    task_id='discovery_job',
    application_id='<your-app-id>',
    execution_role_arn='<your-role-arn>',
    job_driver={
        'sparkSubmit': {
            'entryPoint': 's3://your-bucket/pipeline-files/01_discovery_job.py'
        }
    },
    dag=dag
)
```

## Troubleshooting

### "S3_BUCKET must be configured"

**Cause:** Placeholder value not replaced

**Fix:** Edit the script and change line 40/95:
```python
S3_BUCKET = "spark-history-123456789012-us-east-1"  # Your bucket
```

### "HMS_URI must be configured"

**Cause:** HMS URI not set

**Fix:** Edit `04_json_to_iceberg_enhanced.py` line 42:
```python
HMS_URI = "thrift://hms.example.com:9083"  # Your HMS
```

### "Table not found: backlog_events_log"

**Cause:** Backlog table doesn't exist

**Fix:** Create the table using the SQL in section 2 above

### "No logs to process from backlog"

**Cause:** Discovery job hasn't run or no new logs

**Fix:** 
1. Run discovery job first: `spark-submit 01_discovery_job.py`
2. Check S3_PREFIX matches your event log location
3. Query backlog table: `SELECT COUNT(*) FROM ... WHERE is_processed='N'`

## IAM Permissions Required

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket",
        "s3:PutObject"
      ],
      "Resource": [
        "arn:aws:s3:::your-bucket/*",
        "arn:aws:s3:::your-bucket"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "glue:GetDatabase",
        "glue:GetTable",
        "glue:CreateTable",
        "glue:UpdateTable",
        "glue:GetPartitions"
      ],
      "Resource": "*"
    }
  ]
}
```

## Updating from Git

When you pull updates from Git:

1. **Pull changes:**
   ```bash
   git pull origin main
   ```

2. **Review USER CONFIGURATION sections** (lines 40-150 in each file)

3. **Merge your values** with any new variables

4. **Re-upload to S3:**
   ```bash
   aws s3 cp . s3://${S3_BUCKET}/${S3_PREFIX}/ --recursive --exclude "*" --include "*.py"
   ```

The USER CONFIGURATION section is clearly marked - only that section needs review when updating.

## Best Practices

1. **Run discovery job frequently** (every 30 min) to keep backlog current
2. **Run orchestrator less frequently** (every 1-2 hours) to batch process
3. **Monitor backlog table** for growing pending count
4. **Use separate orchestrators** for regular and large files
5. **Clean old records** from backlog table after 90 days
6. **Set up alerts** for high failure rates
7. **Review recommendations** periodically for optimization opportunities

## Architecture Decisions

### Why Backlog Table?

- ✅ Prevents duplicate processing (each log processed once)
- ✅ Fault tolerance (can resume from failures)
- ✅ Parallel processing (multiple instances can process different batches)
- ✅ Audit trail (track which logs processed and when)
- ✅ Priority handling (can prioritize certain logs)

### Why Two Orchestrators?

- **Regular** (`02_orchestrator_backlog.py`): 500 batch size for <1GB logs
- **Large** (`02_orchestrator_backlog-1gb_plus_events.py`): 5 batch size for >=1GB logs

This prevents memory issues with large files while maintaining high throughput for regular files.

## Support

For issues or questions:
1. Check configuration (all placeholder values replaced?)
2. Verify tables exist (backlog and output tables)
3. Check IAM permissions
4. Review CloudWatch/EMR logs for errors
5. Query backlog table for processing status

## License

This pipeline is provided as-is for use with AWS EMR Serverless.

---

**Note:** All scripts use in-file configuration for easy setup and updates. Look for the "USER CONFIGURATION" section at the top of each script (lines 40-150) to customize for your environment.
