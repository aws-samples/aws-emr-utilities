---
name: troubleshoot-spark-workload
description: Troubleshoot a Spark workload
argument-hint: "<execution-id> <execution-type-id> --profile <name> --region <region> [--platform <type>]"
allowed-tools:
  - Bash
  - AskUserQuestion
  - Glob
---

# Analyze Spark Workload

Use the Spark workload analysis script to analyze the specified workload. The script uses a Strands Agent with MCP tools to perform deep analysis and generate code fix recommendations.

## Arguments

Parse the user's input to extract:
- **execution-id** (required): The first positional argument
  - For Glue: Job Run ID (e.g., `jr_abc123`)
  - For EMR: Step ID (e.g., `s-1234567890ABC`)
  - For EMR Serverless: Job Run ID (UUID format)
- **execution-type-id** (required): The second positional argument
  - For Glue: Job Name
  - For EMR: Cluster ID (e.g., `j-ABC123`)
  - For EMR Serverless: Application ID
- **--platform** (optional): Platform type - `glue`, `emr_serverless`, or `emr_ec2`
  - If not provided, infer from the ID patterns
- **--profile** (optional): AWS profile name for the account containing the workload
- **--region** (optional): AWS region (defaults to us-east-1)

## Pre-Execution: Gather Missing Information (REQUIRED)

**IMPORTANT**: Before running the analysis script, you MUST use the AskUserQuestion tool to gather any missing required information. Do NOT proceed with default values without confirming with the user.

### Step 1: Validate Region and Profile

If `--region` is NOT provided in the arguments:
- Use AskUserQuestion to ask: "Which AWS region is your workload located in?"
- Provide common options: us-east-1, us-west-2, eu-west-1, plus "Other" for custom input

If `--profile` is NOT provided in the arguments:
- Use AskUserQuestion to ask: "Do you need to use a specific AWS profile to access this workload?"
- If user indicates yes, ask for the profile name

### Step 2: Validate Platform Type

If `--platform` is not specified, determine it automatically:
- If execution-id starts with `jr_` → `glue`
- If execution-type-id starts with `j-` → `emr_ec2`
- If execution-id looks like a UUID and execution-type-id has `app` pattern → `emr_serverless`
- If unclear, use AskUserQuestion to ask the user to specify the platform

## Execution

### Step 1: Locate the Script

Use the Glob tool to find the analysis script:

```
Pattern: **/spark-troubleshooting-claude-plugin/scripts/troubleshoot_spark_workload.py
```

The script is located in the `scripts/` directory of the `spark-troubleshooting-claude-plugin` folder within the workspace.

### Step 2: Run the Analysis

Once you have the full script path, run:

```bash
uv run <full-script-path> \
  --execution-id <execution-id> \
  --execution-type-id <execution-type-id> \
  --platform-type <platform-type> \
  --profile <profile> \
  --region <region>
```

**IMPORTANT**:
- Use the FULL ABSOLUTE PATH to the script (e.g., `/Volumes/workplace/.../scripts/troubleshoot_spark_workload.py`)
- Do NOT use `$CLAUDE_PLUGIN_ROOT` - this environment variable is not reliably set
- Always include `--profile` and `--region` with the values gathered from the user

Wait for the analysis to complete (may take up to 3 minutes) and present the results.

## Output Format

Present the analysis results with:
- Summary of the workload analysis
- Root cause of any issues
- Recommendations for fixes
- Code diff (if generated)

## Error Handling

If the script returns an error:
- Explain what went wrong in user-friendly terms
- Suggest possible causes (invalid IDs, permissions, workload not found)
- Recommend next steps for resolution

## Examples

```
/spark-analysis:troubleshoot-spark-workload jr_abc123 my-glue-job --profile prod-account --region us-west-2
/spark-analysis:troubleshoot-spark-workload s-1234567890ABC j-CLUSTERID123 --platform emr_ec2 --profile prod-account --region us-west-2
/spark-analysis:troubleshoot-spark-workload jr_def456 my-job --profile prod-account --region us-west-2
```
