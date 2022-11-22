# Trino Autoscaling

## Introduction

This package contains a script that publishes Trino JMX metrics from EMR clusters to CloudWatch. The metrics can be used by EMR automatic scaling policies to enable the cluster to scale up and down. For more details, see "Tip 8: Advanced scaling using automatic scaling based on custom Presto metrics" in [Top 9 performance tuning tips for PrestoDB on Amazon EMR](https://aws.amazon.com/blogs/big-data/top-9-performance-tuning-tips-for-prestodb-on-amazon-emr/).

**Note:** If you're using Presto instead of Trino, use the scripts that begin with `presto_` instead of `trino_`

**IMPORTANT:** You may notice new nodes are not utilized by queries already in progress (while scaling out). You can read more about why [here](https://docs.qubole.com/en/latest/admin-guide/engine-admin/presto-admin/autoscaling.html). Depending on your query/job pattern, for example if you have one expensive query, you may want to manually scale out the cluster before submitting the query.

## Installation

1. Copy `trino_cloudwatch_ba.sh` and `trino_cloudwatch.sh` to S3.

2. Change the bucket name in `trino_cloudwatch_ba.sh` (line 8).

3. Add `trino_cloudwatch_ba.sh` as an EMR bootstrap action (BA) script. This BA script will download `trino_cloudwatch.sh` and run it as a scheduled job (every 30 seconds). The script publishes Trino metrics to CloudWatch.

4. Create EMR cluster with the following classification: 
```
[{"classification":"trino-connector-jmx", "properties":{"connector.name":"jmx"}, "configurations":[]}]
```
For Presto, use the following:
```
[{"classification":"presto-connector-jmx", "properties":{"connector.name":"jmx"}, "configurations":[]}]
```

5. Add the following autoscaling rules to the EMR cluster. These are just examples and can be changed based on cluster usage patterns:

![image](autoscaling_rules.png)

## Limitations

1. As of EMR 5.19.0, custom metrics for autoscaling cannot be entered from the EMR Console, so you will need to use the AWS CLI to launch the EMR cluster. Also, you cannot create a new cluster by cloning an existing cluster that uses custom metrics, as doing so will break autoscaling using custom metrics. So for now, always use the AWS CLI to launch a new cluster for autoscaling using custom metrics.

2. Once a cluster is launched, the autoscaling policy cannot be changed or detached and reattached, as doing so will disable the autoscaling. If you need to change the autoscaling policy, you will need to launch a new cluster with the new policy using the AWS CLI.

## Example Configuration

Below is an example of launching an EMR cluster with a rule to scale in task nodes based on `PrestoNumQueuedQueries`. The autoscaling configuration is in `example_autoscaling_config.json`. See the AWS documentation for further details on [automatic scaling with custom policies](https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-automatic-scaling.html).
```
aws emr create-cluster \
  --os-release-label 2.0.20221004.0 \
  --applications Name=Hadoop Name=Hive Name=Pig Name=Hue Name=Presto \
  --ec2-attributes '{"InstanceProfile":"EMR_EC2_DefaultRole","SubnetId":"subnet-1","EmrManagedSlaveSecurityGroup":"sg-1","EmrManagedMasterSecurityGroup":"sg-1"}' \
  --release-label emr-5.36.0 \f
  --log-uri 's3n://aws-logs-<AWS_ACCOUNT_NUMBER>-us-west-2/elasticmapreduce/' \
  --configurations '[{"Classification":"presto-connector-jmx","Properties":{"connector.name":"jmx"}}]' \
  --auto-scaling-role EMR_AutoScaling_DefaultRole \
  --bootstrap-actions '[{"Path":"s3://<BUCKET_NAME_CHANGE_ME>/presto_cloudwatch_ba.sh","Name":"Custom action"}]' \
  --ebs-root-volume-size 10 \
  --service-role EMR_DefaultRole \
  --enable-debugging \
  --auto-termination-policy '{"IdleTimeout":3600}' \
  --name 'Example Scale-In Cluster' \
  --scale-down-behavior TERMINATE_AT_TASK_COMPLETION \
  --region us-west-2 \
  --instance-groups file://example_autoscaling_config.json
```
