# EMR Serverless Spark Config Advisor — MCP Server

An [MCP](https://modelcontextprotocol.io) server that analyzes Spark event logs and generates optimized EMR Serverless configurations. Works with [Kiro](https://kiro.dev), Claude Desktop, Amazon Q CLI, and any MCP-compatible client.

## Quick Start

```bash
git clone https://github.com/aws-samples/aws-emr-utilities.git
cd aws-emr-utilities/mcp-servers/emr-serverless-spark-advisor
./deploy-cfn.sh --region us-east-1
```

This deploys a CloudFormation stack with an S3 bucket, IAM roles, EMR Serverless application, Lambda function, and Function URL. Stack outputs include ready-to-paste MCP config.

Then add the MCP config to your client (e.g. `~/.kiro/settings/mcp.json`):

```json
{
  "mcpServers": {
    "spark-config-advisor": {
      "command": "python3",
      "args": ["spark_advisor_mcp.py"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "EMR_SERVERLESS_APP_ID": "<from stack output>",
        "EMR_EXECUTION_ROLE": "<from stack output>",
        "SCRIPT_S3_PATH": "s3://<artifacts-bucket>/spark-advisor-artifacts/spark_extractor.py",
        "ARCHIVES_S3_PATH": "s3://<artifacts-bucket>/spark-advisor-artifacts/zstandard.zip",
        "OUTPUT_S3_PATH": "s3://<advisor-bucket>/advisor-output"
      }
    }
  }
}
```

## What It Does

```
You: "Analyze my Spark logs at s3://my-bucket/event-logs/"

AI → submits parallel EMR Serverless jobs → extracts metrics → returns:
  12 apps analyzed in 153s
  Top cost: app-001 (idle: 86%, 1.2 TB spill)

You: "Generate config recommendations"

AI → returns:
  Worker: Large (16 vCPU, 108 GB) | Max executors: 24
  spark.sql.shuffle.partitions: 512
  spark.emr-serverless.executor.disk: 1500G
  ⚠ WindowGroupLimit skew detected
```

## Tools

| Tool | Description |
|------|-------------|
| `analyze_spark_logs` | Extract metrics from S3 event logs via EMR Serverless |
| `generate_emr_serverless_config_recommendations` | Full Spark configs: worker sizing, shuffle, disk, bottleneck warnings |
| `list_event_log_prefixes` | Browse S3 for available event logs |
| `list_applications` | List extracted apps with summary metrics |
| `get_application` | Full metrics + config for one app |
| `get_bottlenecks` | Severity-ranked findings: CPU, memory, spill, shuffle, idle cores |
| `compare_job_performance` | Side-by-side metrics with % deltas |
| `compare_job_environments` | Diff Spark configs between two apps |
| `list_slowest_stages` | Top N stages by duration |
| `get_stage_details` | Deep dive into one stage |
| `get_resource_timeline` | Executor scaling events over time |
| `list_sql_executions` | SQL queries with duration |
| `compare_sql_execution_plans` | Diff physical plans between two queries |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `EMR_SERVERLESS_APP_ID` | Yes | EMR Serverless application ID |
| `EMR_EXECUTION_ROLE` | Yes | IAM role ARN for job execution |
| `SCRIPT_S3_PATH` | Yes | S3 path to `spark_extractor.py` |
| `ARCHIVES_S3_PATH` | No | S3 path to `zstandard.zip` (for zstd-compressed logs) |
| `OUTPUT_S3_PATH` | Yes | S3 base path for extracted output |
| `MCP_TRANSPORT` | No | `streamable-http` (default/Lambda) or `stdio` (local) |

## Architecture

1. MCP client sends `analyze_spark_logs` with an S3 path
2. Server discovers apps, submits 1 EMR Serverless job per app (parallel)
3. Each job runs `spark_extractor.py` — extracts 80+ metrics from event logs
4. Server reads results from S3, generates recommendations
5. All subsequent tools (`get_bottlenecks`, `compare_*`, etc.) query cached S3 data

No SSH, no EMR EC2 cluster, no infrastructure to manage.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Missing config: EMR_SERVERLESS_APP_ID` | Set all required env vars |
| No event log apps found | S3 path should contain `eventlog_v2_*` subdirectories |
| EMR Serverless job FAILED | Check CloudWatch logs for the job run |
| `ModuleNotFoundError: zstandard` | Set `ARCHIVES_S3_PATH` to zstandard.zip built for Python 3.9 |
| Application not found | Run `analyze_spark_logs` first to extract data |

## License

Apache License 2.0
