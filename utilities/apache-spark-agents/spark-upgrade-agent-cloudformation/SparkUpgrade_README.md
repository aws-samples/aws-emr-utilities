# Apache Spark Upgrade Agent for Amazon EMR

## Overview

The Apache Spark Upgrade Agent for Amazon EMR is a conversational AI capability that accelerates Apache Spark version upgrades for your EMR applications. Traditional Spark upgrades require months of engineering effort to analyze API changes, resolve dependency conflicts, and validate functional correctness. The agent simplifies the upgrade process through natural language prompts, automated code transformation, and data quality validation.

You can use the agent to upgrade PySpark and Scala applications running on Amazon EMR on EC2 and Amazon EMR Serverless. The agent analyzes your code, identifies required changes, and performs automated transformations while maintaining your approval control over all modifications.

For further reference on architecture and detailed information, check https://docs.aws.amazon.com/emr/latest/ReleaseGuide/spark-upgrades.html

## Setup Instructions

### Prerequisites

Before we begin our setup process, make sure you have the following installed on your workstation: 

- AWS CLI - https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
- Python 3.10 or above - https://www.python.org/downloads/
- Uv - https://docs.astral.sh/uv/getting-started/installation/
- Kiro CLI - https://kiro.dev/docs/cli/installation/
- AWS local credentials configured (via AWS CLI, environment variables, or IAM roles) - for local operations such as uploading upgraded job artifacts for EMR validation job execution

Setup instructions for each of the aforementioned MCP servers can be found below. 

### Apache Spark Upgrade Agent Setup

#### Step 1: Deploy Cloudformation stack

```bash
aws cloudformation deploy \
  --template-file spark-upgrade-mcp-setup.yaml \
  --stack-name spark-mcp-setup \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ExecutionRoleToGrantS3Access=EMR_EC2_DefaultRole
```

**Note:** The `ExecutionRoleToGrantS3Access` parameter is required when creating a new bucket. Replace `EMR_EC2_DefaultRole` with your actual EMR job execution role name or ARN.

**With custom parameters:**

```bash
aws cloudformation deploy \
  --template-file spark-upgrade-mcp-setup.yaml \
  --stack-name spark-mcp-setup \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    SparkUpgradeIAMRoleName=spark-upgrade-role \
    StagingBucketPath=my-existing-bucket/spark-upgrade \
    EnableEMREC2=false \
    EnableEMRServerless=true
```

**Enable specific services only:**

```bash
# EMR-EC2 only
aws cloudformation deploy \
  --template-file spark-upgrade-mcp-setup.yaml \
  --stack-name spark-mcp-setup \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    EnableEMREC2=true \
    EnableEMRServerless=false

# EMR-Serverless only
aws cloudformation deploy \
  --template-file spark-upgrade-mcp-setup.yaml \
  --stack-name spark-mcp-setup \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    EnableEMREC2=false \
    EnableEMRServerless=true
```

**With KMS encryption:**

If you want to use KMS encryption instead of default S3 encryption (AES256):

```bash
aws cloudformation deploy \
  --template-file spark-upgrade-mcp-setup.yaml \
  --stack-name spark-mcp-setup \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    UseS3Encryption=true
```

**Using existing S3 bucket with KMS key:**

If you already have an S3 bucket with a KMS key:

```bash
aws cloudformation deploy \
  --template-file spark-upgrade-mcp-setup.yaml \
  --stack-name spark-mcp-setup \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    StagingBucketPath=s3://my-existing-bucket/spark-upgrade \
    UseS3Encryption=true \
    S3KmsKeyArn=arn:aws:kms:us-east-1:111122223333:key/a1b2c3d4-5678-90ab-cdef-EXAMPLE11111
```

**With EMR-Serverless CloudWatch Logs KMS encryption:**

If your EMR-Serverless application encrypts CloudWatch Logs with a customer-managed KMS key:

```bash
aws cloudformation deploy \
  --template-file spark-upgrade-mcp-setup.yaml \
  --stack-name spark-mcp-setup \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    CloudWatchKmsKeyArn=arn:aws:kms:us-east-1:111122223333:key/a1b2c3d4-5678-90ab-cdef-EXAMPLE22222
```

**Note:** The `CloudWatchKmsKeyArn` parameter is only needed if you've configured your EMR-Serverless application to use a customer-managed KMS key for CloudWatch Logs encryption. You can find this key ARN in your EMR-Serverless application's CloudWatch Log group configuration settings.

**With EMR-Serverless S3 logging:**

If your EMR-Serverless application stores logs in S3 instead of (or in addition to) CloudWatch Logs:

```bash
aws cloudformation deploy \
  --template-file spark-upgrade-mcp-setup.yaml \
  --stack-name spark-mcp-setup \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    EnableEMRServerless=true \
    EMRServerlessS3LogPath=s3://my-logs-bucket/emr-serverless-logs
```

**Note:** This parameter is only used when `EnableEMRServerless=true`. It grants the Spark Upgrade role read-only access to your EMR-Serverless S3 logs, allowing it to analyze job failures and provide troubleshooting recommendations.

**Granting S3 access to your EMR job execution role (Required for new buckets):**

When creating a new staging bucket, you must provide your EMR job execution role so it can access the staging artifacts. You can provide either a role name or role ARN - the template automatically detects the format:

```bash
# Option 1: Using role name (simplest)
aws cloudformation deploy \
  --template-file spark-upgrade-mcp-setup.yaml \
  --stack-name spark-mcp-setup \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ExecutionRoleToGrantS3Access=EMR_EC2_DefaultRole

# Option 2: Using role ARN (without path)
aws cloudformation deploy \
  --template-file spark-upgrade-mcp-setup.yaml \
  --stack-name spark-mcp-setup \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ExecutionRoleToGrantS3Access=arn:aws:iam::111122223333:role/EMR_EC2_DefaultRole

# Option 3: Using role ARN with path
aws cloudformation deploy \
  --template-file spark-upgrade-mcp-setup.yaml \
  --stack-name spark-mcp-setup \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ExecutionRoleToGrantS3Access=arn:aws:iam::111122223333:role/iam/EMR_EC2_DefaultRole
```

**Important:** 
- This parameter is **required** when creating a new staging bucket (StagingBucketPath is empty)
- If you specify an existing bucket via `StagingBucketPath`, this parameter is optional and no inline policy will be added
- Without this parameter, your EMR jobs won't be able to access the staging bucket for upgrade artifacts

**Parameter options:**
- `SparkUpgradeIAMRoleName` - Name of the IAM role to create for Spark upgrades (leave empty to auto-generate with stack name for uniqueness, allowing multiple stack deployments)
- `StagingBucketPath` - S3 path for staging artifacts. Supports multiple formats:
  - Empty string (default): Auto-generates a new bucket as `spark-upgrade-{short-id}`
  - Bucket only: `my-bucket` (grants access to entire bucket)
  - Bucket with path: `my-bucket/spark-upgrade` (grants access only to this path)
  - S3 URI: `s3://my-bucket/spark-upgrade` (grants access only to this path)
- `EnableEMREC2` - Enable EMR-EC2 upgrade permissions (default: true)
- `EnableEMRServerless` - Enable EMR-Serverless upgrade permissions (default: true)
- `UseS3Encryption` - Enable KMS encryption for S3 staging bucket (default: false, set to true to use KMS encryption instead of default S3 AES256 encryption)
- `S3KmsKeyArn` - (Optional) ARN of existing KMS key for S3 bucket encryption. Only used if UseS3Encryption is true and you have an existing bucket with a KMS key
- `CloudWatchKmsKeyArn` - (Optional) ARN of the KMS key used to encrypt EMR-Serverless CloudWatch Logs. Only required if your EMR-Serverless application is configured to encrypt logs with a customer-managed KMS key. Leave empty if using default encryption or AWS-managed keys
- `EMRServerlessS3LogPath` - (Optional) S3 path where EMR-Serverless application logs are stored. Only used when `EnableEMRServerless=true`. Supports formats: `s3://bucket/path` or `bucket/path`
- `ExecutionRoleToGrantS3Access` - **Required when creating a new bucket**. IAM Role Name or ARN of your EMR-EC2 or EMR-Serverless job execution role that needs access to the staging bucket. This grants your Spark jobs permission to read/write upgrade artifacts. Supports both simple role names and ARNs with paths. The template automatically detects the format and extracts the role name when needed. Leave empty only if using an existing bucket (StagingBucketPath provided)

**Note:** By default, resource names are auto-generated using the stack name (for IAM role) and a short ID derived from the stack ID (for S3 bucket), which allows you to deploy multiple instances of this stack in the same account and region without conflicts. The S3 bucket name format is `spark-upgrade-{8-char-id}` to ensure lowercase naming and length compliance.

After deployment completes, copy and run the export command from the CloudFormation output to set your environment variables.

For Example:

```bash
export SMUS_MCP_REGION=us-east-1 && export IAM_ROLE=arn:aws:iam::111122223333:role/spark-upgrade-role-xxxxxx && export STAGING_BUCKET=amzn-s3-spark-upgrade-demo-bucket
```

#### Step 2: Configure AWS CLI profile

```bash
aws configure set profile.spark-upgrade-profile.role_arn ${IAM_ROLE}
aws configure set profile.spark-upgrade-profile.region ${SMUS_MCP_REGION}
aws configure set profile.spark-upgrade-profile.source_profile <AWS CLI Profile to assume the IAM role - ex: default>
```

#### Step 3: Configure MCP Server

##### Kiro CLI (Recommended)
If you are using Kiro CLI, use the following command to add the MCP configuration:

```bash
kiro-cli-chat mcp add \
    --name "spark-upgrade" \
    --command "uvx" \
    --args "[\"mcp-proxy-for-aws@latest\",\"https://sagemaker-unified-studio-mcp.${SMUS_MCP_REGION}.api.aws/spark-upgrade/mcp\", \"--service\", \"sagemaker-unified-studio-mcp\", \"--profile\", \"spark-upgrade-profile\", \"--region\", \"${SMUS_MCP_REGION}\", \"--read-timeout\", \"180\"]" \
    --timeout 180000 \
    --scope global
```

##### Manual Configuration
Alternatively, you can manually configure the MCP server with your preferred GenAI conversational assistant/IDE. For Kiro CLI, the MCP configuration is located in `~/.kiro/settings/mcp.json`. If you are using some other conversational assistant, consult their documentation for specific path.

The configuration requires us to provide region information. For **US East (N. Virginia) - `us-east-1`**, the configuration will be: 
```json
{
  "mcpServers": {
    "spark-upgrade": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "mcp-proxy-for-aws@latest",
        "https://sagemaker-unified-studio-mcp.us-east-1.api.aws/spark-upgrade/mcp",
        "--service",
        "sagemaker-unified-studio-mcp",
        "--profile",
        "spark-upgrade-profile",
        "--region",
        "us-east-1",
        "--read-timeout",
        "180"
      ],
      "timeout": 180000,
      "disabled": false
    }
  }
}
```

Update the region name in SigV4 Endpoint and the `--region` parameter if you wish to use a region other than `us-east-1`.

##### Other IDEs and Tools

**Amazon Q Developer CLI (Legacy)**
If you are using Amazon Q Developer CLI and have not migrated to Kiro CLI, we strongly recommend you to upgrade. That said, you can still use the MCP server with Q CLI. To configure the MCP server, use the following shell command:

```bash
qchat mcp add \
    --name "spark-upgrade" \
    --command "uvx" \
    --args "[\"mcp-proxy-for-aws@latest\",\"https://sagemaker-unified-studio-mcp.${SMUS_MCP_REGION}.api.aws/spark-upgrade/mcp\", \"--service\", \"sagemaker-unified-studio-mcp\", \"--profile\", \"spark-upgrade-profile\", \"--region\", \"${SMUS_MCP_REGION}\", \"--read-timeout\", \"180\"]" \
    --timeout 180000 \
    --scope global
```

If you wish to modify the configuration manually, you can update the configuration file located at `~/.aws/amazonq/mcp.json`. If you just want to add the MCP server at the workspace level, you can add the configuration to `.amazonq/mcp.json`

**Amazon Q Developer IDE Plugin**

Amazon Q Developer in the IDE uses the configuration file located at `~/.aws/amazonq/default.json` to load MCP servers. The filename `mcp.json` is supported as well (for legacy reasons). Support for legacy `mcp.json` files is enabled by the `useLegacyMcpJson` field in your global `default.json` config file. By default, this field is set to true. Refer [AWS Q Developer documentation](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html) for more information.

**Kiro IDE**

Kiro allows us to configure MCP at the workspace level or at the user level. Kiro IDE uses the same MCP configuration file as Kiro CLI. To configure MCP for the user, modify `~/.kiro/settings/mcp.json` to include the MCP configuration above. If you just want to include the MCP server to a specific workspace, add the MCP configuration to `.kiro/settings/mcp.json` in your workspace. Refer [Amazon Kiro's documentation](https://kiro.dev/docs/mcp/) for more information on MCP support with Amazon Kiro.

**Cline**

To use the MCP Server with Cline, click on the **MCP Servers** icon on the top right corner of the chat window, click Configure tab and click Configure MCP Servers to open `cline_mcp_settings.json` file. Add the above JSON configuration and save the file.

Your MCP Server will appear in the Cline MCP Server list where you can see a Green/Yellow/Red status indicator next to the server name indicating the server status.

![Cline MCP Setup](../docs/Images/cline_mcp_setup.png)

**Claude Desktop**

To use the MCP Server with Claude Desktop, modify the configuration file to include the MCP configuration. The file path varies depending on your operating system:

- MacOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

### Setup Validation 

You can launch a Kiro CLI Chat session to confirm MCP server is loading correctly. Use `/mcp` command in Kiro to check the status of initialized MCP servers, `/tools` command to list MCP tools available for use and `/tools schema` to list tools along with input schema.

#### Troubleshooting

If you need to test the MCP server connection manually, you can run:

```bash
SMUS_MCP_REGION=us-east-1
uvx mcp-proxy-for-aws@latest https://sagemaker-unified-studio-mcp.${SMUS_MCP_REGION}.api.aws/spark-upgrade/mcp --service sagemaker-unified-studio-mcp --profile spark-upgrade-profile --region ${SMUS_MCP_REGION} --log-level DEBUG --read-timeout 180
```

#### Example Kiro CLI Session 

```bash
❯ kiro-cli-chat
○ fetch is disabled
⚠ 0 of 1 mcp servers initialized. Servers still loading:
 - spark-upgrade
⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀     ⢀⣴⣶⣶⣦⡀⠀⠀⠀⢀⣴⣶⣦⣄⡀⠀⠀⢀⣴⣶⣶⣦⡀⠀⠀⢀⣴⣶⣶⣶⣶⣶⣶⣶⣶⣶⣦⣄⡀⠀⠀⠀⠀⠀⠀⢀⣠⣴⣶⣶⣶⣶⣶⣦⣄⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀    ⢰⣿⠋⠁⠈⠙⣿⡆⠀⢀⣾⡿⠁⠀⠈⢻⡆⢰⣿⠋⠁⠈⠙⣿⡆⢰⣿⠋⠁⠀⠀⠀⠀⠀⠀⠀⠀⠈⠙⠻⣦⠀⠀⠀⠀⣴⡿⠟⠋⠁⠀⠀⠀⠈⠙⠻⢿⣦⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀    ⢸⣿⠀⠀⠀⠀⣿⣇⣴⡿⠋⠀⠀⠀⢀⣼⠇⢸⣿⠀⠀⠀⠀⣿⡇⢸⣿⠀⠀⠀⢠⣤⣤⣤⣤⣄⠀⠀⠀⠀⣿⡆⠀⠀⣼⡟⠀⠀⠀⠀⣀⣀⣀⠀⠀⠀⠀⢻⣧⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀    ⢸⣿⠀⠀⠀⠀⣿⡿⠋⠀⠀⠀⢀⣾⡿⠁⠀⢸⣿⠀⠀⠀⠀⣿⡇⢸⣿⠀⠀⠀⢸⣿⠉⠉⠉⣿⡇⠀⠀⠀⣿⡇⠀⣼⡟⠀⠀⠀⣰⡿⠟⠛⠻⢿⣆⠀⠀⠀⢻⣧⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀    ⢸⣿⠀⠀⠀⠀⠙⠁⠀⠀⢀⣼⡟⠁⠀⠀⠀⢸⣿⠀⠀⠀⠀⣿⡇⢸⣿⠀⠀⠀⢸⣿⣶⣶⡶⠋⠀⠀⠀⠀⣿⠇⢰⣿⠀⠀⠀⢰⣿⠀⠀⠀⠀⠀⣿⡆⠀⠀⠀⣿⡆
⠀⠀⠀⠀⠀⠀⠀    ⢸⣿⠀⠀⠀⠀⠀⠀⠀⠀⠹⣷⡀⠀⠀⠀⠀⢸⣿⠀⠀⠀⠀⣿⡇⢸⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣼⠟⠀⢸⣿⠀⠀⠀⢸⣿⠀⠀⠀⠀⠀⣿⡇⠀⠀⠀⣿⡇
⠀⠀⠀⠀⠀⠀⠀    ⢸⣿⠀⠀⠀⠀⠀⣠⡀⠀⠀⠹⣷⡄⠀⠀⠀⢸⣿⠀⠀⠀⠀⣿⡇⢸⣿⠀⠀⠀⠀⣤⣄⠀⠀⠀⠀⠹⣿⡅⠀⠀⠸⣿⠀⠀⠀⠸⣿⠀⠀⠀⠀⠀⣿⠇⠀⠀⠀⣿⠇
⠀⠀⠀⠀⠀⠀⠀    ⢸⣿⠀⠀⠀⠀⣾⡟⣷⡀⠀⠀⠘⣿⣆⠀⠀⢸⣿⠀⠀⠀⠀⣿⡇⢸⣿⠀⠀⠀⠀⣿⡟⣷⡀⠀⠀⠀⠘⣿⣆⠀⠀⢻⣧⠀⠀⠀⠹⣷⣦⣤⣤⣾⠏⠀⠀⠀⣼⡟
⠀⠀⠀⠀⠀⠀⠀    ⢸⣿⠀⠀⠀⠀⣿⡇⠹⣷⡀⠀⠀⠈⢻⡇⠀⢸⣿⠀⠀⠀⠀⣿⡇⢸⣿⠀⠀⠀⠀⣿⡇⠹⣷⡀⠀⠀⠀⠈⢻⡇⠀⠀⢻⣧⠀⠀⠀⠀⠉⠉⠉⠀⠀⠀⠀⣼⡟
⠀⠀⠀⠀⠀⠀⠀    ⠸⣿⣄⡀⢀⣠⣿⠇⠀⠙⣷⡀⠀⢀⣼⠇⠀⠸⣿⣄⡀⢀⣠⣿⠇⠸⣿⣄⡀⢀⣠⣿⠇⠀⠙⣷⡀⠀⠀⢀⣼⠇⠀⠀⠀⠻⣷⣦⣄⡀⠀⠀⠀⢀⣠⣴⣾⠟
⠀⠀⠀⠀⠀⠀⠀    ⠀⠈⠻⠿⠿⠟⠁⠀⠀⠀⠈⠻⠿⠿⠟⠁⠀⠀⠈⠻⠿⠿⠟⠁⠀⠀⠈⠻⠿⠿⠟⠁⠀⠀⠀⠈⠻⠿⠿⠟⠁⠀⠀⠀⠀⠀⠈⠙⠻⠿⠿⠿⠿⠟⠋⠁

╭─────────────────────────────── Did you know? ────────────────────────────────╮
│                                                                              │
│         Get notified whenever Kiro CLI finishes responding. Just run         │
│               kiro-cli settings chat.enableNotifications true                │
│                                                                              │
╰──────────────────────────────────────────────────────────────────────────────╯

Model: Auto (/model to change)


> /mcp

spark-upgrade
▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
[2025:16:22]: ✓ spark-upgrade loaded in 40.26 s



> /tools


Tool                                     Permission
▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔Built-in
- shell                                  not trusted
- read                                   trust working directory
- write                                  not trusted
- introspect                             trusted
- report                                 not trusted
- aws                                    trust read-only commands

spark-upgrade (MCP)
- check_and_update_build_environment     not trusted
- check_and_update_python_environment    not trusted
- check_job_status                       not trusted
- compile_and_build_project              not trusted
- describe_upgrade_analysis              not trusted
- fix_upgrade_failure                    not trusted
- generate_spark_upgrade_plan            not trusted
- get_data_quality_summary               not trusted
- list_upgrade_analyses                  not trusted
- post_build_result                      not trusted
- post_test_result                       not trusted
- post_upgrade_result                    not trusted
- prepare_python_venv_on_emr             not trusted
- reuse_existing_spark_upgrade_plan      not trusted
- run_validation_job                     not trusted
- update_build_configuration             not trusted
```