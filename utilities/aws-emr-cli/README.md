# emr-cli

emr - Amazon EMR Command Line Interface

The  EMR  Command  Line  Interface is a unified tool written in bash that provides utility commands to perform the following operations within an EMR cluster:

- Collect logs from the cluster
- Evaluate cluster configurations to reduce costs and improve performance
- Manage cluster and application frameworks
- Monitor cluster nodes and applications

__WARNING__ This is the first release of the utility, and some functionalities should be considered as experimental.
The use of this software is recommended in a TEST environment only.

## Installation
1. Launch the [scripts/setup-artifact-bucket.sh](scripts/setup-artifact-bucket.sh) to create a private distribution on an S3 bucket that you own.
```
sh scripts/setup-artifact-bucket.sh YOUR_BUCKET_NAME
```

2. Provide the following additional IAM permissions to the EC2 Instance Profile Role. This allows the cli to run commands on cluster nodes using AWS System Manager and provides additional permissions that are not available in the EMR default EC2 roles.
```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "VisualEditor0",
            "Effect": "Allow",
            "Action": [
                "elasticmapreduce:GetManagedScalingPolicy",
                "elasticmapreduce:ListReleaseLabels",
                "elasticmapreduce:ModifyInstanceFleet",
                "elasticmapreduce:ModifyInstanceGroups",
                "ssm:ListCommandInvocations",
                "ssm:SendCommand"
            ],
            "Resource": "*"
        }
    ]
}
```

- Add managed policy AmazonEC2RoleforSSM

3. Launch an Amazon EMR cluster and attach a bootstrap action with the following parameters
- **Location** s3://YOUR_BUCKET_NAME/artifacts/aws-emr-cli/ba-emr_cli.sh
- **Arguments** s3://YOUR_BUCKET_NAME/artifacts/aws-emr-cli/emr-cli-0.1.zip

## Synopsis
```
usage: emr <command> <subcommand> [parameters]

To see help text, you can run:

  emr help
  emr <command> help
  emr <command> <subcommand> help
```

The synopsis for each command shows its parameters and usage.
Optional parameters are shown within square brackets.

## Available Commands

  - apps
  - benchmark
  - cluster
  - hbase
  - hdfs
  - logs
  - news
  - node
  - update
