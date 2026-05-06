---
name: "spark-troubleshooting-agent"
displayName: "Troubleshoot Spark applications on AWS"
description: "Troubleshoot failed Spark applications on AWS EMR, Glue, and SageMaker Unified Studio - analyze failures, identify root causes, get code recommendations. For more info, please see: https://docs.aws.amazon.com/emr/latest/ReleaseGuide/spark-troubleshoot.html"
keywords: ["spark", "emr", "glue", "sagemaker", "troubleshooting", "pyspark", "scala", "performance", "debugging", "aws"]
author: "AWS"
---

# Onboarding

## Step 1: Validate Prerequisites

Before proceeding, ensure the following are installed:

- **AWS CLI**: Required for AWS authentication
  - Verify with: `aws --version`
  - Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html

- **Python 3.10+**: Required for MCP proxy
  - Verify with: `python3 --version`

- **uv package manager**: Required for MCP Proxy for AWS
  - Verify with: `uv --version`
  - Install: https://docs.astral.sh/uv/getting-started/installation/

- **AWS Credentials**: Must be configured
  - Verify with: `aws sts get-caller-identity`
  - Verify this role has permissions to deploy Cloud Formation stacks
  - Identify the AWS Profile that the creds are configured for
    - You can do this with `echo ${AWS_PROFILE}`. No response likely means they are using the `default` profile
    - use this profile later when configuring the `smus-mcp-profile`

**CRITICAL**: If any prerequisites are missing, DO NOT proceed. Install missing components first.

## Step 2: Deploy CloudFormation Stack

**Note**: If the customer already has a role configured with `sagemaker-unified-studio-mcp` permissions, please ignore this step, get the AWS CLI profile for that role from the customer and configure your (Kiro's) mcp.json file (typically located at `~/.kiro/settings/mcp.json`) with that profile. However, it is recommended to deploy the stack as will contain latest updates for permissions and role policies

1. Log into the AWS Console with the role your AWS CLI is configured with - this role must have permissions to deploy Cloud Formation stacks.
1. Navigate to [troubleshooting agent setup page](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/spark-troubleshooting-agent-setup.html#spark-troubleshooting-agent-setup-resources).
1. Deploy Cfn stack / resources to desired region
1. Configure parameters:
   - **TroubleshootingRoleName**: IAM role name
   - **EnableEMREC2**: Enable EMR-EC2 (default: true)
   - **EnableEMRServerless**: Enable EMR Serverless (default: true)  
   - **EnableGlue**: Enable AWS Glue (default: true)
   - **CloudWatchKmsKeyArn**: Optional KMS key for CloudWatch
1. Wait for deployment to succeed.

Region and role ARN will be in the outputs tab. These will be used in the next step

## Step 3: Configure the AWS CLI with the MCP role and Kiro's mcp.json

1. Propmt the user to provide the region and role ARN from the Cfn stack deployment
1. Configure the CLI with the `smus-mcp-profile`
    ```bash
    # run these as a single command so the use doesn't have to approve multiple times
    aws configure set profile.smus-mcp-profile.source_profile <profile with Cloud Formation deployment permissions from earlier>
    aws configure set profile.smus-mcp-profile.role_arn <role arn from Cloud Formation stack, provided by customer>
    aws configure set profile.smus-mcp-profile.region <region from Cloud Formation stack, provided by customer>
    ```
1. then update your MCP json configuration file (typically located at `~/.kiro/settings/mcp.json`) with the region. Only edit your mcp.json, no other local copies, just the one that configures your mcp settings.

# Overview

Troubleshoot failed Apache Spark applications on Amazon EMR, AWS Glue, and Amazon SageMaker Unified Studio through natural language. The agent analyzes failed jobs, identifies root causes, and provides code recommendations.

**Key capabilities:**
- **Failure Analysis**: Analyze failed PySpark and Scala jobs across platforms
- **Root Cause Identification**: Analyze Spark event logs, error stack traces, resource metrics, query plans, and configurations to identify failure causes
- **Code Recommendations**: Get specific code fixes for failed applications (EMR EC2, Glue, SageMaker Unified Studio)
- **Multi-Platform**: EMR EC2, EMR Serverless, Glue, SageMaker Unified Studio

**Note**: Preview service using cross-region inference for AI processing.

## Available MCP Servers

### sagemaker-unified-studio-mcp-troubleshooting
**Connection:** MCP Proxy for AWS
**Authentication:** AWS IAM role assumption
**Timeout:** 180 seconds
**Supported Platforms:** EMR EC2, EMR Serverless, AWS Glue, SageMaker Unified Studio

Analyzes failed Spark workloads by examining:
- Spark event logs and error stack traces
- Resource usage metrics (CPU, memory, I/O, network via CloudWatch)
- Query plans and executor timelines
- Spark configuration parameters
- Provides root cause identification and diagnostic explanations

### sagemaker-unified-studio-mcp-code-rec
**Connection:** MCP Proxy for AWS
**Authentication:** AWS IAM role assumption
**Timeout:** 180 seconds
**Supported Platforms:** EMR EC2, AWS Glue, SageMaker Unified Studio

**Note:** Code recommendations are **not available** for EMR Serverless. Use the troubleshooting server for EMR Serverless failure analysis.

Generates code recommendations for failed PySpark applications:
- Analyzes failed application code and provides specific code fixes
- Returns a code diff with recommended changes
- **PySpark only** — Scala workloads receive troubleshooting analysis but not code recommendations

## Usage Examples

### Basic Troubleshooting Request

```
"My Spark job on EMR failed. Cluster ID: j-1234567890ABC, Step ID: s-1234567890ABC.
Can you analyze what went wrong?"
```

The agent will:
1. Analyze Spark event logs and error stack traces
2. Identify root cause
3. Provide diagnostic explanation

### Request Code Recommendations

```
"Can you suggest code changes to fix this OutOfMemoryError?"
```

The agent will:
1. Analyze the failed application code
2. Provide a code diff with specific fixes

## Common Troubleshooting Scenarios

### OutOfMemory Errors

```
"My EMR Serverless job failed with OutOfMemoryError.
Application ID: 00abcdef12345678, Job Run ID: 00abcdef87654321.
Can you analyze what went wrong?"
```

Agent analyzes:
- Executor memory configuration and resource metrics
- Partition sizes and data skew
- Memory-intensive operations
- Provides root cause analysis and diagnostic explanation

### Failed Glue Job

```
"My Glue job 'daily-etl' failed on run jr_abc123def456abc123def456abc12345.
Can you analyze what went wrong?"
```

Agent analyzes:
- Error stack traces and failure patterns
- Stage execution issues
- Resource utilization problems
- Provides root cause analysis and suggested fixes

### SageMaker Unified Studio

**Note:** SageMaker Unified Studio (SMUS) has built-in troubleshooting capabilities through its own UI, which provides richer context including session information. SMUS users should use the built-in assistant for the best experience. This power is primarily intended for **EMR EC2, EMR Serverless, and Glue** troubleshooting.

## Best Practices

### ✅ Do:

- **Provide specific identifiers** - Include cluster ID, application ID, or job ID
- **Describe symptoms clearly** - Explain observed vs. expected behavior
- **Include error messages** - Share exact error text
- **Mention the platform** - Specify EMR EC2, EMR Serverless, Glue, or SageMaker
- **Ask follow-up questions** - Dig deeper into recommendations
- **Test in dev first** - Validate changes before production
- **Provide context** - Share data sizes, job frequency, requirements
- **Ask about trade-offs** - Understand performance vs. cost

### ❌ Don't:

- **Skip validation** - Always test code changes before production
- **Ignore root causes** - Address underlying issues, not symptoms
- **Apply blindly** - Consider your specific workload
- **Forget monitoring** - Track metrics after changes
- **Mix issues** - Address one problem at a time
- **Withhold context** - More details = better recommendations

## Troubleshooting

### "Cannot find job or application"
**Cause:** Invalid job ID or inaccessible application
**Solution:**
- Verify job/application ID correct
- Check same region as MCP configuration  
- Ensure IAM role has log access permissions
- Verify logs haven't expired (30 days default for EMR)

### "MCP server timeout"
**Cause:** Analysis exceeds 180-second timeout
**Solution:**
- Break request into smaller parts
- Ask for specific aspects: "analyze memory usage only"
- Request sampling for large jobs: "analyze subset of stages"

### "Insufficient permissions"
**Cause:** IAM role missing permissions
**Solution:**
- Verify CloudFormation stack created successfully in the correct region
- Check that the IAM role ARN in your `smus-mcp-profile` matches the stack outputs
- Re-deploy the CloudFormation stack if needed — it defines all required permissions

### "No recommendations generated"
**Cause:** No actionable code fixes identified for the failure
**Solution:**
- Ask more specific questions about the failure: "what caused the OutOfMemoryError?"
- Provide additional context: error messages, data sizes, job configuration
- Ensure you provided the correct job/step/run identifiers

## MCP Config Placeholders

**IMPORTANT:** Before using this power, replace the following placeholder in `mcp.json` with your actual value:

- **`{AWS_REGION}`**: The AWS region where you deployed the CloudFormation stack and where your Spark jobs run.
  - **How to get it:** This is the region you selected when deploying the CloudFormation stack in Step 2 of the onboarding process
  - **Examples:** `us-east-1`, `us-west-2`, `eu-west-1`, `ap-southeast-1`
  - **Where to find it:** Check the CloudFormation stack outputs tab for the region, or use the same region where your EMR/Glue/SageMaker resources are located

**After replacing the placeholder, your mcp.json should look like:**
```json
{
  "mcpServers": {
    "sagemaker-unified-studio-mcp-troubleshooting": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "mcp-proxy-for-aws@latest",
        "https://sagemaker-unified-studio-mcp.us-west-2.api.aws/spark-troubleshooting/mcp",
        "--service",
        "sagemaker-unified-studio-mcp",
        "--profile",
        "smus-mcp-profile",
        "--region",
        "us-west-2",
        "--read-timeout",
        "180"
      ],
      "timeout": 180000,
      "disabled": false
    }
  }
}
```

## Configuration

**Authentication:** AWS IAM role via CloudFormation stack

**Required Permissions:** Managed by the CloudFormation stack deployed during onboarding. See the stack template for the full list of IAM permissions granted.

**Supported Platforms:**
- Amazon EMR on EC2
- Amazon EMR Serverless
- AWS Glue (Spark jobs)
- Amazon SageMaker Unified Studio (%%pyspark notebook cells — primarily use the built-in SMUS assistant instead)

**Supported Languages:**
- PySpark (Python)
- Scala

---

**Service:** Amazon SageMaker Unified Studio MCP (Preview)  
**Provider:** AWS  
**License:** AWS Service Terms
