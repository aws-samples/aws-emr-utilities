#!/bin/bash
#
# Wait for EMR provisioning to become successful.
#
if [[ $(grep instanceRole /emr/instance-controller/lib/info/job-flow-state.txt | awk '{gsub(/"/, ""); print $2}') == 'master' ]]
then
  while [[ $(sed '/localInstance {/{:1; /}/!{N; b1}; /nodeProvision/p}; d' /emr/instance-controller/lib/info/job-flow-state.txt | sed '/nodeProvisionCheckinRecord {/{:1; /}/!{N; b1}; /status/p}; d' | awk '/SUCCESSFUL/' | xargs) != "status: SUCCESSFUL" ]];
  do
    sleep 1
  done

  # Update the Amazon CloudWatch Agent configuration file to have the prometheus endpoint
  EMR_CLUSTER_METRICS_CONFIG=emr-amazon-cloudwatch-agent.json
  EMR_CLUSTER_METRICS_CONFIG_PATH=/etc/emr-cluster-metrics/amazon-cloudwatch-agent/conf/$EMR_CLUSTER_METRICS_CONFIG
  TMP_EMR_CLUSTER_METRICS_CONFIG_PATH=/tmp/$EMR_CLUSTER_METRICS_CONFIG

  jq --arg AMP_REMOTE_WRITE_ENDPOINT "$1" '.metrics += {"metrics_destinations":{"prometheus_remote_write":{"endpoint":$AMP_REMOTE_WRITE_ENDPOINT,"tls":{"insecure":true}}}}' $EMR_CLUSTER_METRICS_CONFIG_PATH > $TMP_EMR_CLUSTER_METRICS_CONFIG_PATH
  sudo cp -f $TMP_EMR_CLUSTER_METRICS_CONFIG_PATH $EMR_CLUSTER_METRICS_CONFIG_PATH
  sudo rm -rf $TMP_EMR_CLUSTER_METRICS_CONFIG_PATH
  sudo /usr/bin/amazon-cloudwatch-agent-ctl -m ec2 -a stop
  sudo /usr/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c file:$EMR_CLUSTER_METRICS_CONFIG_PATH
fi
exit 0
