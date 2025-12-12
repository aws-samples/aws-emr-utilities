# Spark Troubleshooting Agent Airflow Integration

## Prerequisites

1. The solution file: `spark_failure_analysis.py` 
2. Admin access to your Airflow cluster (to configure connections and permissions)
3. An AWS IAM Role with the following policy (minimum required):

> **Note:** Amazon SageMaker Unified Studio MCP server is in preview and is subject to change.

```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "UsingSMUSMCP",
            "Effect": "Allow",
            "Action": [
                "sagemaker-unified-studio-mcp:InvokeMcp",
                "sagemaker-unified-studio-mcp:CallReadOnlyTool",
                "sagemaker-unified-studio-mcp:CallPrivilegedTool"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream"
            ],
            "Resource": "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-7-sonnet-*"
        }
    ]
}
```

## Setup

1. Create a Slackbot and add it to your desired channel by following the instructions listed here: [https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/](https://docs.slack.dev/app-management/quickstart-app-settings). Be sure to save the webhook URL created after the Slackbot joins your channel.

![Slack Incoming Webhooks Setup](./Images/slack-bot-webhook-auth.jpg)

2. Create a SlackWebhookConnection in your Airflow cluster with your webhook URL. You can do this by navigating to the connections tab in your Airflow UI and creating a new connection from there.
3. Upload the solution file: `spark_failure_analysis.py`  to your Airflow cluster inside the `dags` folder.

## Usage

For any Operator for which you’d like to enable automatic troubleshooting analysis, add the following line to your operator definition:

```
from dags.spark_failure_analysis import spark_failure_callback

with DAG(...) as dag:
    my_task = EmrServerlessStartJobOperator(
        ...,
        on_failure_callback=spark_failure_callback(),
    )
    
    # Or, if you'd like to disable agentic mode
    my_task = EmrServerlessStartJobOperator(
        ...,
        on_failure_callback=spark_failure_callback(agent=False),
    )
```

After making this change, every task failure for the configured tasks will trigger the Spark Troubleshooting Agent and send the consolidated analysis to your configured Slack channel 🎉.
