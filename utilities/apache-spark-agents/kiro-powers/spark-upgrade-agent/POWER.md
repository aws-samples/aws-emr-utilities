---
name: "spark-upgrade-agent"
displayName: "Upgrade Spark applications on AWS"
description: "Accelerate Apache Spark version upgrades with conversational AI that automates code transformation, dependency resolution, and validation testing. Supports PySpark and Scala on EMR EC2 and EMR Serverless."
keywords: ["spark", "emr", "upgrade", "migration", "automation", "code-transformation", "pyspark", "scala", "aws"]
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
  - Install: https://www.python.org/downloads/release/python-3100/

- **uv package manager**: Required for MCP Proxy for AWS
  - Verify with: `uv --version`
  - Install: https://docs.astral.sh/uv/getting-started/installation/

- **AWS Credentials**: Must be configured
  - Verify with: `aws sts get-caller-identity`
  - Configure via AWS CLI, environment variables, or IAM roles
  - Identify the AWS Profile that the creds are configured for
    - You can do this with `echo ${AWS_PROFILE}`. No response likely means they are using the `default` profile
    - use this profile later when configuring the `spark-upgrade-profile`

- **Local Spark project**: Your Spark application source code must be available on the local filesystem
  - The upgrade agent reads and modifies files locally — it does not work on remote repositories directly
  - Clone or check out your project to a local directory before starting
  - **Recommended**: Commit your current code to version control before starting so you can easily review or revert changes

**CRITICAL**: If any prerequisites are missing, DO NOT proceed. Install missing components first.

## Step 2: Deploy CloudFormation Stack

**Note**: If the customer already has a role configured with `sagemaker-unified-studio-mcp` permissions, please ignore this step, get the AWS CLI profile for that role from the customer and configure your (Kiro's) mcp.json file (typically located at `~/.kiro/settings/mcp.json`) with that profile. However, it is recommended to deploy the stack as will contain latest updates for permissions and role policies

1. Log into the AWS Console with the role your AWS CLI is configured with - this role must have permissions to deploy Cloud Formation stacks.
1. Navigate to [upgrade agent setup page](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-spark-upgrade-agent-setup.html#spark-upgrade-agent-setup-resources).
1. Deploy Cfn stack / resources to desired region
1. **Configure parameters** on the "Specify stack details" page:
  - **CloudWatchKmsKeyArn**: (Optional) ARN of the KMS key used to encrypt EMR-Serverless CloudWatch Logs
  - **EMRServerlessS3LogPath**: (Optional) S3 path where EMR-Serverless application logs are stored
  - **EnableEMREC2**: Enable EMR-EC2 upgrade permissions
  - **EnableEMRServerless**: Enable EMR-Serverless upgrade permissions
  - **ExecutionRoleToGrantS3Access**: IAM Role Name or ARN of your EMR-EC2/EMR-Serverless job execution role that needs S3 staging bucket access
  - **S3KmsKeyArn**: (Optional) ARN of existing KMS key for S3 staging bucket encryption. Only used if UseS3Encryption is true and you have an existing bucket with a KMS key
  - **SparkUpgradeIAMRoleName**: Name of the IAM role to create for Spark upgrades. Leave empty to auto-generate with stack name for uniqueness
  - **StagingBucketPath**: S3 path for staging artifacts
  - **UseS3Encryption**: Enable KMS encryption for S3 staging bucket. Set to true to use KMS encryption instead of default S3 encryption
1. **Wait for deployment** to complete successfully.

## Step 3: Configure the AWS CLI with the MCP role and Kiro's mcp.json

1. Propmt the user to provide the region and role ARN from the Cfn stack deployment
1. Configure the CLI with the `spark-upgrade-profile`
    ```bash
    # run these as a single command so the use doesn't have to approve multiple times
    aws configure set profile.spark-upgrade-profile.source_profile <profile with Cloud Formation deployment permissions from earlier>
    aws configure set profile.spark-upgrade-profile.role_arn <role arn from Cloud Formation stack, provided by customer>
    aws configure set profile.spark-upgrade-profile.region <region from Cloud Formation stack, provided by customer>
    ```
1. then update your MCP json configuration file (typically located at `~/.kiro/settings/mcp.json`) with the region. Only edit your mcp.json, no other local copies, just the one that configures your mcp settings.

# Overview

The Apache Spark Upgrade Agent for Amazon EMR is a conversational AI capability that accelerates Apache Spark version upgrades for your EMR applications. Traditional Spark upgrades require months of engineering effort to analyze API changes, resolve dependency conflicts, and validate functional correctness. The agent simplifies the upgrade process through natural language prompts, automated code transformation, and data quality validation.

**Key capabilities:**
- **Upgrade Planning**: Analyzes project structure and generates a comprehensive upgrade plan
- **Build & Dependency Updates**: Resolves version conflicts and updates build configurations
- **Iterative Failure Fixing**: Analyzes build and runtime failures and suggests targeted fixes
- **Validation Testing**: Submits and monitors remote validation jobs on EMR
- **Data Quality Validation**: Compares output between source and target Spark versions
- **Multi-Platform Support**: EMR EC2 and EMR Serverless
- **Multi-Language Support**: PySpark (Python) and Scala

**Note**: Preview service using cross-region inference for AI processing.

## How It Works

The upgrade agent orchestrates a **chained workflow** — all tools are called sequentially, sharing a single `analysis_id` generated in the planning step. You provide a single prompt to start the upgrade, and the agent handles the entire process:

1. **Planning** — Analyzes project structure, identifies dependencies, and generates a comprehensive upgrade plan
2. **Build Configuration** — Updates build files (pom.xml, build.sbt, requirements.txt, etc.) and resolves dependency conflicts
3. **Environment Setup** — Checks and updates Java or Python environment for the target Spark version
4. **Build & Test** — Compiles the project, runs local tests, and iteratively fixes build failures
5. **Validation Jobs** — Submits the upgraded application to EMR for remote validation and monitors execution
6. **Data Quality** — Compares output between source and target Spark versions (when enabled)
7. **Summary** — Generates a comprehensive upgrade report documenting all changes made

## Available MCP Server

### spark-upgrade
**Connection:** MCP Proxy for AWS
**Authentication:** AWS IAM role assumption via spark-upgrade-profile
**Timeout:** 180 seconds

Provides a set of chained upgrade tools including:
- Upgrade plan generation and reuse
- Build configuration and dependency updates
- Java and Python environment checks
- Build guidance and failure analysis
- Remote validation job submission and status monitoring (EMR EC2, EMR Serverless)
- Data quality comparison between source and target versions
- Upgrade result tracking and history

## Usage

The upgrade agent is designed as a **single-prompt workflow**. You describe your upgrade goal, and the agent orchestrates all the steps automatically. You do **not** need to call individual capabilities separately — the tools chain together from a single starting prompt.

### Starting an Upgrade

Provide your current version, target version, and project path. Optionally include your EMR cluster/application ID and S3 staging path:

**PySpark example (EMR on EC2):**
```
"I want to upgrade my PySpark application from EMR 6.10.0 to 7.8.0.
The project is at /path/to/my/spark/project.
My EMR cluster is j-1234567890ABC.
S3 staging path: s3://my-bucket/spark-upgrade/"
```

**Scala example (EMR Serverless):**
```
"I need to upgrade my Scala Spark application from EMR 6.0.0 to 7.0.0.
The project is at /path/to/my/scala/project.
I'm using EMR Serverless, application ID: 00abcdef12345678,
execution role: arn:aws:iam::123456789012:role/my-emr-serverless-role.
S3 staging path: s3://my-bucket/spark-upgrade/"
```

### What the Agent Does Automatically

Once you provide the starting prompt, the agent runs through the full workflow:

1. **Generates an upgrade plan** — Scans your project structure, identifies dependencies, detects build system (Maven, SBT, or Python), and creates a step-by-step upgrade plan
2. **Updates build configuration** — Modifies build files and resolves dependency version conflicts for the target Spark version
3. **Sets up environment** — Checks and updates your Java or Python environment as needed
4. **Builds and tests** — Compiles the project, runs local tests if available, and iteratively fixes build failures using failure analysis
5. **Submits validation jobs** — Packages and submits your application to EMR (EC2 or Serverless) for remote validation
6. **Monitors execution** — Polls job status until completion, reporting progress
7. **Validates data quality** — If enabled, runs your job on both source and target Spark versions and compares outputs (schema, row counts, statistical summaries)
8. **Generates summary** — Produces a comprehensive report of all changes, validation results, and any remaining issues

You will be asked to review and approve changes at key points throughout the process.

## Supported Scenarios

The upgrade agent works on a **single project** at a time. Provide your project path, current version, and target version.

**Supported upgrade paths:**
- PySpark applications (Python) — with pip/requirements.txt, setup.py, pyproject.toml, or Pipfile
- Scala/Java Spark applications — with Maven (pom.xml) or SBT (build.sbt)

**Supported validation platforms:**
- Amazon EMR on EC2 (provide cluster ID)
- Amazon EMR Serverless (provide application ID)

**Note:** For complex projects with multiple modules or shared libraries, consider upgrading each module separately as individual projects.

## Best Practices

- **Review changes before approving** — The agent will propose code and build file modifications. Read the diffs before accepting them.
- **Use a non-production EMR cluster with sample data** — Point validation jobs at a staging cluster, not your production environment. Use sample mock datasets that represent your production data but are smaller in size.
- **Check third-party dependencies** — The agent updates Spark and related libraries, but verify that your other dependencies (e.g., custom JARs, internal libraries) are compatible with the target Spark version.
- **Enable data quality validation** — If your job writes output data, enable the data quality check to compare results between source and target Spark versions.

## Troubleshooting

The upgrade agent has **self-healing capability** — when it encounters build failures, code issues, or validation errors, it iteratively analyzes the failure and applies fixes automatically. The scenarios below are expected parts of the upgrade process, not terminal errors.

### "Project analysis failed"
**Cause:** Unable to access or parse project structure
**Note:** This is the only scenario that requires user action — verify that the project path is correct, accessible, and contains valid build files (pom.xml, build.sbt, requirements.txt, etc.).

### Build failures, code errors, and validation failures
**These are expected.** The agent will automatically:
1. Analyze the failure using the upgrade tools
2. Identify the root cause and relevant Spark migration rules
3. Apply targeted fixes to your code or build configuration
4. Re-build and re-test iteratively until the issue is resolved

If the agent cannot resolve an issue after multiple attempts, it will surface the problem and ask for your input.

## Configuration

**Authentication:** AWS IAM role via CloudFormation stack (spark-upgrade-profile)

**Required Permissions:** Managed by the CloudFormation stack deployed during onboarding. See the stack template for the full list of IAM permissions granted.

**Supported Platforms:**
- Amazon EMR on EC2
- Amazon EMR Serverless

**Supported Languages:**
- PySpark (Python)
- Scala

**Supported Build Systems:**
- Maven (pom.xml)
- SBT (build.sbt)
- Python (requirements.txt, setup.py, pyproject.toml, Pipfile, poetry.lock, conda)

---

**Service:** Amazon SageMaker Unified Studio MCP (Preview)  
**Provider:** AWS  
**License:** AWS Service Terms