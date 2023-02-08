#!/bin/bash

# Author: suthan@amazon.com
# Date: 2023-02-02
# This script will be helps to determine the clusters which are idle for the requested time and also shares the information on the number of worker nodes(core + task) running on EMR currently.

# Prompt user for the desired report age
echo "Enter the desired report age in hours:"
read report_age


# Calculate the start and end times for the report
start_time=$(date --date="$report_age hours ago" +%Y-%m-%dT%H:%M:%S)
end_time=$(date +%Y-%m-%dT%H:%M:%S)

# Get the list of active EMR clusters
clusters=$(aws emr list-clusters --active | jq -r '.Clusters[].Id')

for cluster in $clusters; do
  # echo "Cluster ID: $cluster"

  # Retrieve EMR MRActiveNodes data from CloudWatch
  MRActiveNodes=$(aws cloudwatch get-metric-data \
    --metric-data-queries \
    '[{"Id": "m1", "MetricStat": {"Metric": {"Namespace": "AWS/ElasticMapReduce", "MetricName": "MRActiveNodes", "Dimensions": [{"Name": "JobFlowId", "Value": "'"$cluster"'"}]}, "Period": 300, "Stat": "Maximum"}, "ReturnData": true}]' \
    --start-time $start_time \
    --end-time $end_time | jq -r '.MetricDataResults[].Values[0]')
  # echo "MRActiveNodes: $MRActiveNodes"

  
  # Retrieve EMR IsIdle data from CloudWatch

emr_idle_data=$(aws cloudwatch get-metric-data \
    --metric-data-queries \
    '[{"Id": "m1", "MetricStat": {"Metric": {"Namespace": "AWS/ElasticMapReduce", "MetricName": "IsIdle", "Dimensions": [{"Name": "JobFlowId", "Value": "'"$cluster"'"}]}, "Period": 300, "Stat": "Minimum"}, "ReturnData": true}]' \
    --start-time $start_time \
    --end-time $end_time --output text)

# Filter data to only include metric values
metric_values=$(echo "$emr_idle_data" | awk '{print $2}' | tail -n +4)

# Get the minimum value to check if it has reported false for IsIdle during this time period.
min_value=$(echo "$metric_values" | sort -n | head -n 1)

# Condition check and if it has not reported false for IsIDle during this time, we can consider the cluster was idle completely.
if [ "$min_value" == "1.0" ]; then
    echo "Cluster $cluster remained idle for the last $report_age hours and has $MRActiveNodes worker nodes currently running"
  fi

done

