# IAM Permissions Required

Minimum IAM permissions needed to execute the EMR AL1 Migration Skill.

## Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EMRReadAccess",
      "Effect": "Allow",
      "Action": [
        "elasticmapreduce:DescribeCluster",
        "elasticmapreduce:ListInstanceGroups",
        "elasticmapreduce:ListInstanceFleets",
        "elasticmapreduce:ListSteps",
        "elasticmapreduce:DescribeStep",
        "elasticmapreduce:ListBootstrapActions",
        "elasticmapreduce:ListClusters"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EMRWriteAccess",
      "Effect": "Allow",
      "Action": [
        "elasticmapreduce:RunJobFlow",
        "elasticmapreduce:AddJobFlowSteps",
        "elasticmapreduce:TerminateJobFlows"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3ScriptAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::SCRIPT_BUCKET",
        "arn:aws:s3:::SCRIPT_BUCKET/*"
      ]
    },
    {
      "Sid": "S3StagingBucketAccess",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::emr-migration-staging-*",
        "arn:aws:s3:::emr-migration-staging-*/*"
      ]
    },
    {
      "Sid": "CloudWatchLogsRead",
      "Effect": "Allow",
      "Action": [
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
        "logs:GetLogEvents",
        "logs:FilterLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:log-group:/aws-emr/*"
    },
    {
      "Sid": "EC2Describe",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeInstanceTypeOfferings"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IAMReadRole",
      "Effect": "Allow",
      "Action": [
        "iam:GetRole",
        "iam:GetInstanceProfile"
      ],
      "Resource": [
        "arn:aws:iam::*:role/EMR_DefaultRole",
        "arn:aws:iam::*:role/EMR_EC2_DefaultRole",
        "arn:aws:iam::*:instance-profile/EMR_EC2_DefaultRole"
      ]
    },
    {
      "Sid": "IAMPassRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::*:role/EMR_DefaultRole",
        "arn:aws:iam::*:role/EMR_EC2_DefaultRole"
      ],
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "elasticmapreduce.amazonaws.com"
        }
      }
    }
  ]
}
```

## Additional: Spark Upgrade Agent Permissions (optional)

Required only when using the SageMaker Unified Studio Spark Upgrade Agent MCP server for application code upgrades (Stage 3A). These are provisioned by the CloudFormation stack `spark-upgrade-mcp-setup`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SparkUpgradeAgentMCP",
      "Effect": "Allow",
      "Action": [
        "sagemaker:InvokeEndpoint"
      ],
      "Resource": "arn:aws:sagemaker:*:*:endpoint/spark-upgrade-*"
    },
    {
      "Sid": "SparkUpgradeStagingBucket",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::STAGING_BUCKET",
        "arn:aws:s3:::STAGING_BUCKET/*"
      ]
    },
    {
      "Sid": "SparkUpgradeEMRSubmit",
      "Effect": "Allow",
      "Action": [
        "elasticmapreduce:AddJobFlowSteps",
        "elasticmapreduce:DescribeStep",
        "elasticmapreduce:ListSteps"
      ],
      "Resource": "*"
    }
  ]
}
```

## Notes

- Replace `SCRIPT_BUCKET` with the actual S3 bucket used for bootstrap scripts and JARs.
- Replace `STAGING_BUCKET` with the S3 bucket used by the Spark Upgrade Agent for artifacts.
- `iam:PassRole` is scoped to EMR default roles and restricted via `iam:PassedToService` to EMR only. Adjust the resource ARN to match your environment's naming convention.
- For dry-run mode only, the `EMRWriteAccess` statement is not required.
- No `*FullAccess` policies are used — this follows least-privilege.
- The Spark Upgrade Agent CloudFormation stack creates its own IAM role with necessary permissions. The above is for reference only if provisioning manually.