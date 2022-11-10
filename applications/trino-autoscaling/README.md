# Trino Autoscaling

## Introduction

This package contains a script that will enable autoscaling for Trino EMR clusters. The script pushes Trino JMX metrics to Cloudwatch which are used by EMR autoscaling to scale up and down. 

## Installation

1. Copy trino_cloudwatch_ba.sh and trino_cloudwatch.sh to S3

2. Change the bucket name in trino_cloudwatch_bootstrap.sh (line 7)

3. Add trino_cloudwatch_bootstrap.sh as an EMR bootstrap action (BA) script. This BA script will download trino_cloudwatch.sh and run it as a scheduled job ( every 30 seconds). The script publishes Trino metrics to Cloudwatch

4. Create EMR cluster with the following Classification: [{"classification":"trino-connector-jmx", "properties":{"connector.name":"jmx"}, "configurations":[]}]

5. Add the following autoscaling rules to EMR cluster. These are just examples and can be hanged based on clusters usage patterns

![image](autoscaling_rules.png)


## Limitations

1. As of EMR 5.19.0 custom metrics can not be entered from the EMR Console, so you will need to use the AWS CLI to launch EMR cluster, and you cannot create a new cluster by cloning an existing cluster that uses custom metrics, doing so will break autoscaling using custom metrics. So for now, always use AWS CLI to launch a new cluster for autoscaling using custom metrics.

2. Once cluster is launched, autoscaling policy cannot be changed or detached and reattached, doing so will disable the autoscaling. If you need to change the autoscaling policy, you will need to launch a new cluster with the new policy using AWS CLI.

3. If using Presto, modify trino_cloudwatch.sh and replace all "trino" occurences with "presto"
