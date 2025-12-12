# Apache Spark Agents for Amazon EMR

This repository contains Apache Spark Agents that provide AI-powered assistance for Apache Spark workloads running on Amazon EMR. These agents use Model Context Protocol (MCP) to deliver conversational AI capabilities for Spark application management.

## Available Apache Spark Agents

### 1. Apache Spark Upgrade Agent for Amazon EMR
Assists with upgrading Apache Spark applications across different Spark versions on EMR-EC2 and EMR-Serverless platforms.

**Features:**
- Automated Spark version upgrade planning and execution
- Compatibility analysis and migration recommendations
- Build environment validation and updates
- Automated testing and validation

📖 **[View Spark Upgrade Setup Guide](spark-upgrade-agent-cloudformation/SparkUpgrade_README.md)**

### 2. Apache Spark Troubleshooting Agent for Amazon EMR
The Apache Spark Troubleshooting Agent provides AI-powered troubleshooting capabilities for Apache Spark applications running on AWS Glue, EMR-EC2, and EMR-Serverless. It also includes code recommendation capabilities for Spark application optimization.

#### Features
**Troubleshooting Capabilities:**
- Analyze failed Spark jobs and identify root causes
- Retrieve and analyze logs from EMR clusters, EMR-Serverless applications, and Glue jobs
- Get recommendations for fixing common Spark issues
- Diagnose performance problems and optimization opportunities

**Code Recommendation Capabilities:**
- Code analysis and optimization suggestions
- Best practices recommendations
- Performance improvement guidance

📖 **[View Spark Troubleshooting & Code Recommendation Setup Guide](docs/setup/spark-troubleshooting-agent-cloudformation-setup.md)**

## Quick Start

Each Apache Spark Agent has its own setup guide with detailed instructions:

### 1. Apache Spark Upgrade Agent
- **[CloudFormation Setup Guide](spark-upgrade-agent-cloudformation/SparkUpgrade_README.md)** - Deploy IAM roles and configure the MCP server

### 2. Apache Spark Troubleshooting Agent
- **[CloudFormation Setup Guide](docs/setup/spark-troubleshooting-agent-cloudformation-setup.md)** - Deploy IAM roles and configure the MCP server
- **[Airflow Integration](docs/spark-troubleshooting-agent-airflow-integration.md)** - Integrate troubleshooting with Airflow workflows
- **EventBridge Integration** - Automate troubleshooting notifications for failed Spark jobs:
  - [CloudFormation Setup](docs/spark-troubleshooting-agent-eventbridge-integration-cloudformation.md)
  - [CLI Setup](docs/spark-troubleshooting-agent-eventbridge-integration-cli.md)

## Prerequisites

Before setting up any Apache Spark Agent, ensure you have:

- AWS CLI - https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
- Python 3.10 or above - https://www.python.org/downloads/
- Uv - https://docs.astral.sh/uv/getting-started/installation/
- Kiro CLI - https://kiro.dev/docs/cli/installation/
- AWS local credentials configured (via AWS CLI, environment variables, or IAM roles) - for local operations

## Architecture

Each Apache Spark Agent follows a similar architecture:

1. **CloudFormation Stack** - Deploys IAM roles and necessary AWS resources
2. **AWS CLI Profile** - Configures credentials for assuming the IAM role
3. **Agent Configuration** - Connects your IDE/CLI to the agent endpoint via MCP
