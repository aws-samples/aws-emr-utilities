#!/bin/bash
#
# This script configures metrics for all nodes
# Waits for EMR provisioning to become successful.
#
while [[ $(sed '/localInstance {/{:1; /}/!{N; b1}; /nodeProvision/p}; d' /emr/instance-controller/lib/info/job-flow-state.txt | sed '/nodeProvisionCheckinRecord {/{:1; /}/!{N; b1}; /status/p}; d' | awk '/SUCCESSFUL/' | xargs) != "status: SUCCESSFUL" ]];
do
  sleep 1
done
#
# Now the EMR cluster is ready.
#

# Configures DataNode metrics
if sudo systemctl status hadoop-hdfs-datanode --no-pager; then
  sudo aws s3 cp s3://my-s3-bucket/datanode-metrics.yaml /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_hdfs_datanode_jmx_mapping.yaml
  sudo sed -i -e "s/REPLACE_ME_INSTANCEID/$(ec2-metadata -i | cut -d " " -f 2)/g" /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_hdfs_datanode_jmx_mapping.yaml
  sudo sed -i -e "s/REPLACE_ME_CLUSTERID/$(jq -r ".jobFlowId" < /emr/instance-controller/lib/info/extraInstanceData.json)/g" /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_hdfs_datanode_jmx_mapping.yaml
  sudo systemctl stop hadoop-hdfs-datanode
  sudo systemctl start hadoop-hdfs-datanode
fi

# Configures NameNode metrics
if sudo systemctl status hadoop-hdfs-namenode --no-pager; then
  sudo aws s3 cp s3://my-s3-bucket/namenode-metrics.yaml /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_hdfs_namenode_jmx_mapping.yaml
  sudo sed -i -e "s/REPLACE_ME_INSTANCEID/$(ec2-metadata -i | cut -d " " -f 2)/g" /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_hdfs_namenode_jmx_mapping.yaml
  sudo sed -i -e "s/REPLACE_ME_CLUSTERID/$(jq -r ".jobFlowId" < /emr/instance-controller/lib/info/extraInstanceData.json)/g" /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_hdfs_namenode_jmx_mapping.yaml
  sudo systemctl stop hadoop-hdfs-namenode
  sudo systemctl start hadoop-hdfs-namenode
fi

# Configures NodeManager metrics
if sudo systemctl status hadoop-yarn-nodemanager --no-pager; then
  sudo aws s3 cp s3://my-s3-bucket/nodemanager-metrics.yaml /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_yarn_nodemanager_jmx_mapping.yaml
  sudo sed -i -e "s/REPLACE_ME_INSTANCEID/$(ec2-metadata -i | cut -d " " -f 2)/g" /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_yarn_nodemanager_jmx_mapping.yaml
  sudo sed -i -e "s/REPLACE_ME_CLUSTERID/$(jq -r ".jobFlowId" < /emr/instance-controller/lib/info/extraInstanceData.json)/g" /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_yarn_nodemanager_jmx_mapping.yaml
  sudo systemctl stop hadoop-yarn-nodemanager
  sudo systemctl start hadoop-yarn-nodemanager
fi

# Configures ResourceManager metrics
if sudo systemctl status hadoop-yarn-resourcemanager --no-pager; then
  sudo aws s3 cp s3://my-s3-bucket/resourcemanager-metrics.yaml /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_yarn_resourcemanager_jmx_mapping.yaml
  sudo sed -i -e "s/REPLACE_ME_INSTANCEID/$(ec2-metadata -i | cut -d " " -f 2)/g" /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_yarn_resourcemanager_jmx_mapping.yaml
  sudo sed -i -e "s/REPLACE_ME_CLUSTERID/$(jq -r ".jobFlowId" < /emr/instance-controller/lib/info/extraInstanceData.json)/g" /etc/emr-cluster-metrics/adot-java-agent/conf/hadoop_yarn_resourcemanager_jmx_mapping.yaml
  sudo systemctl stop hadoop-yarn-resourcemanager
  sudo systemctl start hadoop-yarn-resourcemanager
fi

exit 0
