#!/bin/bash
#
# This script configures system metrics for all cluster nodes
# Wait for EMR provisioning to become successful.
#
while [[ $(sed '/localInstance {/{:1; /}/!{N; b1}; /nodeProvision/p}; d' /emr/instance-controller/lib/info/job-flow-state.txt | sed '/nodeProvisionCheckinRecord {/{:1; /}/!{N; b1}; /status/p}; d' | awk '/SUCCESSFUL/' | xargs) != "status: SUCCESSFUL" ]];
do
  sleep 1
done
#
# Now the EMR cluster is ready.
#
sudo systemctl stop amazon-cloudwatch-agent.service

# Update <my-s3-bucket> to be the bucket in your account that holds these bootstrap actions.
sudo aws s3 cp s3://my-s3-bucket/emr-amazon-cloudwatch-agent.json /etc/emr-cluster-metrics/amazon-cloudwatch-agent/conf/emr-amazon-cloudwatch-agent.json
sudo sed -i -e "s/REPLACE_ME_INSTANCEID/$(ec2-metadata -i | cut -d " " -f 2)/g" /etc/emr-cluster-metrics/amazon-cloudwatch-agent/conf/emr-amazon-cloudwatch-agent.json
sudo sed -i -e "s/REPLACE_ME_CLUSTERID/$(jq -r ".jobFlowId" < /emr/instance-controller/lib/info/extraInstanceData.json)/g" /etc/emr-cluster-metrics/amazon-cloudwatch-agent/conf/emr-amazon-cloudwatch-agent.json
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -s -m ec2 -c file:/etc/emr-cluster-metrics/amazon-cloudwatch-agent/conf/emr-amazon-cloudwatch-agent.json

exit 0
