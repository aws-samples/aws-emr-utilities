## Steps - Configuring Spark Troubleshooting Notification Flow via CLI

### Step 1. Create SNS Topic and subscribe to the topic

```
*# Create SNS topic*
aws sns create-topic --name spark-analysis-notifications --region REGION

*# Subscribe to the topic (replace with your AWS Account ID, region and email)*
aws sns subscribe \
    --topic-arn arn:aws:sns:REGION:YOUR_ACCOUNT:spark-analysis-notifications \
    --protocol email \
    --notification-endpoint your-email@example.com \
    --region REGION
```

After running the subscribe command, you will receive an email from `AWS Notifications <no-reply@sns.amazonaws.com>` with the following content:

![EventBridge Subscription Confirmation Email](./Images/spark-eventbridge-notification-subscription-confirmation-email.png)

Click the "Confirm subscription".

### Step 2. Build the Lambda Deployment Package

The [Lambda function](../spark-troubleshooting-agent-notification-integration/event-bridge-integration/lambda-function.py) code is being provided in this GitHub repository, you can build it by running the [build_lambda_package.sh](../spark-troubleshooting-agent-notification-integration/event-bridge-integration/build_lambda_package.sh) script. This script creates a ZIP file with all dependencies needed for the Lambda deployment package.


### Step 3. Upload the Lambda Deployment Package to your S3 bucket

```
*# Replace YOUR_BUCKET with your S3 bucket name*
aws s3 cp spark-analysis-lambda.zip \
    s3://YOUR_BUCKET/lambda-packages/spark-analysis-lambda.zip
```

### Step 4. Create the Lambda function execution role

The Lambda function requires an IAM role with specific permissions. Create the role with the following policies:

#### 4.1: Create Trust Policy

Create a file named `lambda-trust-policy.json`:

```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": "lambda.amazonaws.com"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}

```

#### 4.2: Create the IAM Role

```
aws iam create-role \
    --role-name spark-analysis-lambda-role \
    --assume-role-policy-document file://lambda-trust-policy.json
```

#### 4.3: Attach AWS Managed Policy for Basic Lambda Execution

This provides CloudWatch Logs permissions:

```
aws iam attach-role-policy \
    --role-name spark-analysis-lambda-role \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

#### 4.4: Create and Attach SMUS MCP Policy

Create a file named `smus-mcp-policy.json`:

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
        }
    ]
}
```

Attach the policy:

```
aws iam put-role-policy \
    --role-name spark-analysis-lambda-role \
    --policy-name SMUSMCPPolicy \
    --policy-document file://smus-mcp-policy.json
```

#### 4.5: Add SNS Publish Permission

Create a file named `sns-publish-policy.json`: (replace REGION and YOUR_ACCOUNT) 

```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "sns:Publish"
            ],
            "Resource": "arn:aws:sns:*:*:spark-analysis-notifications"
        }
    ]
}
```

Attach the policy:

```
aws iam put-role-policy \
    --role-name spark-analysis-lambda-role \
    --policy-name SNSPublishPolicy \
    --policy-document file://sns-publish-policy.json
```

### 4.6: Add Spark Troubleshooting Permissions (Platform-Specific)

Choose the appropriate policy based on your deployment mode, see https://docs.aws.amazon.com/emr/latest/ReleaseGuide/spark-troubleshooting-agent-iam-setup.html

### Step 5. Create the Lambda Function and configure environment variables

```
*# Create the Lambda function using S3 location*
aws lambda create-function \
    --function-name spark-workload-analysis \
    --runtime python3.13 \
    --role arn:aws:iam::YOUR_ACCOUNT:role/spark-analysis-lambda-role \
    --handler lambda_function.lambda_handler \
    --code S3Bucket=YOUR_BUCKET,S3Key=lambda-packages/spark-analysis-lambda.zip \
    --timeout 300 \
    --memory-size 512
*# Update the environment variable*
aws lambda update-function-configuration \
    --function-name spark-workload-analysis \
    --environment "Variables={SNS_TOPIC_ARN=arn:aws:sns:REGION:YOUR_ACCOUNT:spark-analysis-notifications}"
    
*# To update the function code later:*
aws lambda update-function-code \
    --function-name spark-workload-analysis \
    --s3-bucket YOUR_BUCKET \
    --s3-key lambda-packages/spark-analysis-lambda.zip
    
```

### Step 6. Set up EventBridge Rule

#### 6.1 Create IAM role for allowing EventBridge Rule to invoke Lambda function

We need to firstly create an IAM role that can be used by the EventBridge Rule to invoke Lambda. Create a file: `eventbridge-trust-policy.json`:

```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "TrustEventBridgeService",
            "Effect": "Allow",
            "Principal": {
                "Service": "events.amazonaws.com"
            },
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {
                    "aws:SourceArn": "arn:aws:events:REGION:YOUR_ACCOUNT:rule/emr-ec2-step-status-changes-to-failed",
                    "aws:SourceAccount": "YOUR_ACCOUNT"
                }
            }
        }
    ]
}
```

And another file: `eventbridge-lambda-policy.json`

```
{
    "Version": "2012-10-17",
    "Statement": [
        {
        "Effect": "Allow",
        "Action": [
            "lambda:InvokeFunction"
        ],
        "Resource": "arn:aws:lambda:REGION:YOUR_ACCOUNT:function:spark-workload-analysis"
        }
    ]
}
```

And create the role:

```
aws iam create-role \
    --role-name eventbridge-lambda-invoke-role \
    --assume-role-policy-document file://eventbridge-trust-policy.json

aws iam put-role-policy \
    --role-name eventbridge-lambda-invoke-role \
    --policy-name LambdaInvokePolicy \
    --policy-document file://eventbridge-lambda-policy.json
```

#### 6.2 Create the EventBridge Rule

For EMR-EC2

```
*# Create rule*
aws events put-rule \
    --name "emr-ec2-step-status-changes-to-failed" \
    --event-pattern "{\"source\": [\"aws.emr\"], \"detail-type\": [\"EMR Step Status Change\"], \"detail\": {\"state\": [\"FAILED\"]}}" \
    --description "EMR Step Status Changes to FAILED" \
    --state "ENABLED" \
    --event-bus-name "default"
    
*# Add Lambda function as target*
aws events put-targets \
    --rule emr-ec2-step-status-changes-to-failed \
    --targets "Id"="1","Arn"="arn:aws:lambda:REGION:YOUR_ACCOUNT:function:spark-workload-analysis","RoleArn"="arn:aws:iam::YOUR_ACCOUNT:role/eventbridge-lambda-invoke-role"
```

For EMR Serverless

```
# Create rule
aws events put-rule \
    --name "emr-serverless-job-status-changes-to-failed" \
    --event-pattern "{\"source\": [\"aws.emr-serverless\"], \"detail-type\": [\"EMR Serverless Job Run State Change\"], \"detail\": {\"state\": [\"FAILED\"]}}" \
    --description "EMR Serverless Job Run State Changes to Failed" \
    --state "ENABLED" \
    --event-bus-name "default"
    
# Add Lambda function as target
aws events put-targets \
    --rule emr-serverless-job-status-changes-to-failed \
    --targets "Id"="1","Arn"="arn:aws:lambda:REGION:YOUR_ACCOUNT:function:spark-workload-analysis","RoleArn"="arn:aws:iam::YOUR_ACCOUNT:role/eventbridge-lambda-invoke-role"
```

For Glue Jobrun

```
# Create rule
aws events put-rule \
    --name "glue-job-status-changes-to-failed" \
    --event-pattern "{\"source\": [\"aws.glue\"], \"detail-type\": [\"`Glue Job State Change`\"], \"detail\": {\"state\": [\"FAILED\"]}}" \
    --description "Glue Job Run State Changes to Failed" \
    --state "ENABLED" \
    --event-bus-name "default"
    
# Add Lambda function as target
aws events put-targets \
    --rule glue-job-status-changes-to-failed \
    --targets "Id"="1","Arn"="arn:aws:lambda:REGION:YOUR_ACCOUNT:function:spark-workload-analysis","RoleArn"="arn:aws:iam::YOUR_ACCOUNT:role/eventbridge-lambda-invoke-role"
```

#### 6.3 Grant EventBridge Permission to Invoke Lambda

EventBridge requires a resource-based policy on the Lambda function to invoke it. Add the appropriate permission based on which rule(s) you created:

For EMR-EC2:

```
aws lambda add-permission \
    --function-name spark-workload-analysis \
    --statement-id AllowEventBridgeInvokeEMREC2 \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn arn:aws:events:REGION:YOUR_ACCOUNT:rule/emr-ec2-step-status-changes-to-failed \
    --region REGION
```

For EMR Serverless:

```
aws lambda add-permission \
    --function-name spark-workload-analysis \
    --statement-id AllowEventBridgeInvokeEMRServerless \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn arn:aws:events:REGION:YOUR_ACCOUNT:rule/emr-serverless-job-status-changes-to-failed \
    --region REGION
```

For Glue Jobrun:

```
aws lambda add-permission \
    --function-name spark-workload-analysis \
    --statement-id AllowEventBridgeInvokeGlue \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn arn:aws:events:REGION:YOUR_ACCOUNT:rule/glue-job-status-changes-to-failed \
    --region REGION
```

### Step 7. (Optional) Set up Slack as target of SNS Topic

For setting up Slack channel and generating the webhook for notification workflow, please check this doc: https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-alertmanager-SNS-otherdestinations.html#AMP-alertmanager-SNS-otherdestinations-Slack which links to an instruction video: https://www.youtube.com/watch?v=CszzQcPAqNM

The command to set up  Slack WebHook as target:

```
aws sns subscribe \
    --topic-arn arn:aws:sns:REGION:YOUR_ACCOUNT:spark-analysis-notifications \
    --protocol HTTPS \
    --notification-endpoint WEBHOOK_URL
```

After running the above command, you will need to go to the SNS console to find the topic `spark-analysis-notifications`, you will see this newly created subscription in Pending status. Select the subscription and click `Request confirmation`. you will receive a message of URL in your Slack channel for confirming the subscription; go back to the SNS console and click `Confirm subscription` and paste the URL.
