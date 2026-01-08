# Spark Analysis Plugin for Claude Code

Analyze and troubleshoot Apache Spark workloads running on AWS Glue, EMR, and EMR Serverless.

## Features

- **Workload Analysis**: Analyze Spark job executions for performance issues and optimization opportunities
- **Code Recommendations**: Automatically generate code fix recommendations when issues are detected
- **Multi-Platform Support**: Works with AWS Glue Jobs, EMR Steps, and EMR Serverless Job Runs
- **Multi-Account Support**: Analyze workloads across different AWS accounts using different profiles
- **Batch Analysis**: Process multiple workloads from spreadsheets or lists with per-workload profile support

## Architecture

This plugin uses a **script-based architecture** that makes direct MCP tool calls for each analysis request. This design enables:

- **No LLM overhead**: Direct MCP calls without intermediate LLM processing
- **Profile isolation**: Each analysis runs in its own process with its own AWS credentials
- **Multi-account support**: Different workloads can use different AWS profiles without affecting Claude Code's Bedrock connection
- **Batch processing**: Analyze multiple workloads from different accounts in sequence

```
Claude Code Agent
       │
       ▼
  Bash Tool (uv run)
       │
       ▼
  troubleshoot_spark_workload.py
       │
       ▼
  Direct MCP Tool Calls
       │
       ▼
  Raw JSON Output → Claude Code parses & presents
```

## Prerequisites

- **AWS Credentials**: Valid AWS credentials with access to the SageMaker Unified Studio MCP service
- **Python/uv**: The `uv` tool must be available (install via `pip install uv` or `pipx install uv`)

## Installation

Install this plugin in Claude Code:

```bash
claude plugin marketplace add aws-samples/aws-emr-utilities
claude plugin install spark-analysis@aws-emr-utilities
```

Or test locally:

```bash
claude --plugin-dir /path/to/spark-analysis-claude-plugin
```

## Usage

### Command: Analyze Spark Workload

```
/spark-analysis:troubleshoot-spark-workload <execution-id> <execution-type-id> [options]
```

**Arguments:**
- `execution-id`: The execution identifier
  - For Glue: Job Run ID (e.g., `jr_abc123`)
  - For EMR: Step ID (e.g., `s-1234567890ABC`)
  - For EMR Serverless: Job Run ID
- `execution-type-id`: The resource identifier
  - For Glue: Job Name
  - For EMR: Cluster ID (e.g., `j-ABC123`)
  - For EMR Serverless: Application ID

**Options:**
- `--platform <type>`: Platform type (`glue`, `emr_serverless`, `emr_ec2`) - auto-detected if not specified
- `--profile <name>`: AWS profile name
- `--region <region>`: AWS region (default: `us-east-1`)

**Examples:**

```bash
# Analyze a Glue job run
/spark-analysis:troubleshoot-spark-workload jr_abc123 my-glue-job

# Analyze an EMR step
/spark-analysis:troubleshoot-spark-workload s-1234567890ABC j-CLUSTERID123

# Analyze with specific profile and region
/spark-analysis:troubleshoot-spark-workload jr_def456 my-job --profile prod-account --region us-west-2
```

### Batch Analysis from Spreadsheets

You can analyze multiple workloads by running the command multiple times. Claude Code can run these in parallel.

For a spreadsheet with columns: `execution_id`, `job_name`, `profile`, `region`:

```bash
# Run multiple analyses (Claude Code handles parallelization)
/spark-analysis:troubleshoot-spark-workload jr_abc123 job-1 --profile account-a --region us-east-1
/spark-analysis:troubleshoot-spark-workload jr_def456 job-2 --profile account-b --region us-west-2
```

## Multi-Account Workflow

### Why Multi-Account Support Matters

In enterprise environments, Spark workloads often span multiple AWS accounts:
- Different accounts for dev/staging/prod
- Different teams with separate accounts
- Cross-account data pipelines

### How It Works

1. Each analysis spawns a new Python process via `uv run`
2. The process creates a boto3 session with the specified profile
3. Direct MCP tool calls are made using those credentials
4. Raw JSON results are returned to Claude Code for presentation

This architecture ensures:
- Claude Code's Bedrock connection remains unaffected
- Each workload can use different credentials
- No credential conflicts between analyses

### Example: Cross-Account Debugging Report

```bash
# Analyze jobs from different accounts
/spark-analysis:troubleshoot-spark-workload jr_abc123 etl-job-1 --profile prod-account --region us-east-1
/spark-analysis:troubleshoot-spark-workload jr_def456 etl-job-2 --profile dev-account --region us-east-1
/spark-analysis:troubleshoot-spark-workload jr_ghi789 etl-job-3 --profile staging-account --region us-east-1
```

## Troubleshooting

### Connection Issues

If you encounter connection errors:

1. Verify your AWS credentials are valid: `aws sts get-caller-identity --profile <profile>`
2. Ensure you have network access to the SageMaker Unified Studio MCP endpoint
3. Check that the `uv` tool is installed: `which uv`

### Permission Errors

Ensure your AWS profile has:
- Access to the SageMaker Unified Studio MCP service

### Timeout Issues

Analysis can take up to 3 minutes per workload. For batch analysis of many workloads, expect proportionally longer total time.

### Script Errors

If the analysis script fails, check:
- AWS credentials are configured correctly for the specified profile
- The execution IDs and type IDs are valid
- Network connectivity to AWS services

## Files

```
spark-analysis-claude-plugin/
├── .claude-plugin/
│   └── plugin.json              # Plugin manifest
├── commands/
│   └── troubleshoot-spark-workload.md    # Slash command
├── scripts/
│   └── troubleshoot_spark_workload.py    # Analysis script (direct MCP calls)
├── .gitignore
└── README.md
```

## Support

For issues and feature requests, please contact the AWS Data Platform team.
