#!/bin/bash -xe

# This script installs and configures Prometheus on the master nodes to collect node level and application level
# (Hadoop and HBase) metrics from all cluster nodes. It can also configured to export metrics to
# your AWS Prometheus workspace via the remote_write endpoint. AWS Prometheus workspace id is an optional argument,
# that, if passed, configures the on-cluster Prometheus instance to export metrics to AWS Prometheus
# Usage in BA: --bootstrap-actions '[{"Path":"s3://<s3_path>/install_emr_metrics_test1.sh","Args":["ws-537c7364-f10f-4210-a0fa-deedd3ea1935"]

#set up node_exporter for pushing OS level metrics
function install_node_exporter() {
  cd /tmp
  sudo useradd --no-create-home --shell /bin/false node_exporter

  if [ "$(uname -m)" = "aarch64" ]; then
    wget https://github.com/prometheus/node_exporter/releases/download/v1.3.1/node_exporter-1.3.1.linux-arm64.tar.gz
    tar -xvzf node_exporter-1.3.1.linux-arm64.tar.gz
    cd node_exporter-1.3.1.linux-arm64
  else
    wget https://github.com/prometheus/node_exporter/releases/download/v1.3.1/node_exporter-1.3.1.linux-amd64.tar.gz
    tar -xvzf node_exporter-1.3.1.linux-amd64.tar.gz
    cd node_exporter-1.3.1.linux-amd64
  fi

  sudo cp node_exporter /usr/local/bin/
  sudo chown node_exporter:node_exporter /usr/local/bin/node_exporter

  cd ..
  cat >node_exporter.service <<EOF
[Unit]
Description=Node Exporter

[Service]
User=node_exporter
Group=node_exporter
ExecStart=/usr/local/bin/node_exporter

[Install]
WantedBy=multi-user.target
EOF
  sudo cp node_exporter.service /etc/systemd/system/node_exporter.service
  sudo chown node_exporter:node_exporter /etc/systemd/system/node_exporter.service
  sudo systemctl daemon-reload
  sudo systemctl start node_exporter
  sudo systemctl enable node_exporter
}

function install_jmx_exporter() {
  wget https://repo1.maven.org/maven2/io/prometheus/jmx/jmx_prometheus_javaagent/0.17.2/jmx_prometheus_javaagent-0.17.2.jar
  sudo mkdir -p /usr/lib/prometheus
  sudo cp jmx_prometheus_javaagent-0.17.2.jar /usr/lib/prometheus
}

function setup_jmx_hadoop() {
  cd /tmp

  wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-observability/conf_files/hdfs_jmx_config_namenode.yaml
  wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-observability/conf_files/hdfs_jmx_config_datanode.yaml
  wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-observability/conf_files/yarn_jmx_config_resource_manager.yaml
  wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-observability/conf_files/yarn_jmx_config_node_manager.yaml


  HADOOP_CONF='/etc/hadoop/conf.empty'
  sudo mkdir -p ${HADOOP_CONF}
  sudo cp hdfs_jmx_config_namenode.yaml ${HADOOP_CONF}
  sudo cp hdfs_jmx_config_datanode.yaml ${HADOOP_CONF}
  sudo cp yarn_jmx_config_resource_manager.yaml ${HADOOP_CONF}
  sudo cp yarn_jmx_config_node_manager.yaml ${HADOOP_CONF}
}

# configure the jmx_exporter for hbase
function setup_jmx_hbase() {
  cat >hbase_jmx_config.yaml <<EOF
lowercaseOutputName: true
lowercaseOutputLabelNames: true
rules:
  - pattern: '.*'
EOF

  # we have to manually load the hbase-env changes to allow the jmx_exporter to push on multiple ports
  cat >hbase-env-master.sh <<"EOF"
export HBASE_OPTS="$HBASE_OPTS -javaagent:/usr/lib/prometheus/jmx_prometheus_javaagent-0.17.2.jar=7005:/etc/hbase/conf/hbase_jmx_config.yaml"
EOF

  cat >hbase-env-thrift.sh <<"EOF"
export HBASE_OPTS="$HBASE_OPTS -javaagent:/usr/lib/prometheus/jmx_prometheus_javaagent-0.17.2.jar=7007:/etc/hbase/conf/hbase_jmx_config.yaml"
EOF

  cat >hbase-env-rest.sh <<"EOF"
export HBASE_OPTS="$HBASE_OPTS -javaagent:/usr/lib/prometheus/jmx_prometheus_javaagent-0.17.2.jar=7008:/etc/hbase/conf/hbase_jmx_config.yaml"
EOF

  cat >hbase-env-regionserver.sh <<"EOF"
export HBASE_OPTS="$HBASE_OPTS -javaagent:/usr/lib/prometheus/jmx_prometheus_javaagent-0.17.2.jar=7006:/etc/hbase/conf/hbase_jmx_config.yaml"
EOF
  sudo mkdir -p /etc/hbase/conf
  sudo cp hbase_jmx_config.yaml /etc/hbase/conf
  sudo cp hbase-env-master.sh /etc/hbase/conf
  sudo cp hbase-env-thrift.sh /etc/hbase/conf
  sudo cp hbase-env-rest.sh /etc/hbase/conf
  sudo cp hbase-env-regionserver.sh /etc/hbase/conf
}

# Download the graphite_exporter_mapping.conf from GitHub repository
function download_graphite_mapping_file() {
  wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-observability/conf_files/graphite_exporter_mapping.conf -O /tmp/graphite_exporter_mapping.conf
}

# Install graphite_exporter
function install_graphite_exporter() {
  download_graphite_mapping_file

  wget https://github.com/prometheus/graphite_exporter/releases/download/v0.14.0/graphite_exporter-0.14.0.linux-amd64.tar.gz
  tar -xzf graphite_exporter-0.14.0.linux-amd64.tar.gz graphite_exporter-0.14.0.linux-amd64/
  cd graphite_exporter-0.14.0.linux-amd64/
  ./graphite_exporter --graphite.mapping-config /tmp/graphite_exporter_mapping.conf &
}

function install_prometheus() {
  if [ "$(uname -m)" = "aarch64" ]; then
    wget https://github.com/prometheus/prometheus/releases/download/v2.40.5/prometheus-2.40.5.linux-arm64.tar.gz
    tar xvf prometheus-2.40.5.linux-arm64.tar.gz
    mv prometheus-2.40.5.linux-arm64 prometheus
  else
    wget https://github.com/prometheus/prometheus/releases/download/v2.40.5/prometheus-2.40.5.linux-amd64.tar.gz
    tar xvf prometheus-2.40.5.linux-amd64.tar.gz
    mv prometheus-2.40.5.linux-amd64 prometheus
  fi

  sudo cp -r prometheus /usr/lib/prometheus

  sudo useradd --no-create-home --shell /bin/false prometheus
  sudo chown -R prometheus:prometheus /usr/lib/prometheus

  JOBFLOWID=$(grep jobFlowId /emr/instance-controller/lib/info/job-flow-state.txt | cut -d\" -f2)
  HOSTNAME=$(hostname)

  cat >prometheus.yml <<EOF
global:
  scrape_interval:  15s

  # Setup external labels to de-dupe metrics in HA cluster https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-ingest-dedupe.html
  external_labels:
    cluster: ${JOBFLOWID}
    __replica__: ${HOSTNAME}

scrape_configs:
  - job_name: "prometheus"
    static_configs:
      - targets: ["localhost:9091"]
    relabel_configs:
    - target_label: instance
      replacement: ${HOSTNAME}
    - target_label: cluster_id
      replacement: ${JOBFLOWID}

  - job_name: 'node'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 9100
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type   

  - job_name: 'hadoop_hdfs_namenode'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 7003
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - MASTER
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type    

  - job_name: 'hadoop_hdfs_datanode'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 7004
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - CORE
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

  - job_name: 'hadoop_yarn_resourcemanager'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 7001
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - MASTER
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

  - job_name: 'hadoop_yarn_nodemanager'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 7002
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - CORE
        - TASK
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

  - job_name: 'hbase_regionserver'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 7006
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - CORE
        - TASK
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

  - job_name: 'hbase_hmaster'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 7005
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - MASTER
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

  - job_name: 'hbase_rest'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 7008
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - MASTER
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

  - job_name: 'hbase_thrift'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 7007
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - MASTER
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type
EOF

  # configure remote_write to AWS Prometheus if AWS Prometheus workspace id is provided
  if [ -n "$AMP_WORKSPACE_ID" ]; then
    tee -a prometheus.yml >/dev/null <<EOT

remote_write:
  - url: https://aps-workspaces.${REGION}.amazonaws.com/workspaces/${AMP_WORKSPACE_ID}/api/v1/remote_write
    sigv4:
         region: ${REGION}
EOT
  fi

  sudo rm /usr/lib/prometheus/prometheus.yml
  sudo mkdir -p /etc/prometheus
  sudo cp prometheus.yml /etc/prometheus/

  sudo chown -R prometheus:prometheus /etc/prometheus

  cat >prometheus.service <<EOF
[Unit]
Description=Prometheus
Wants=network-online.target
After=network-online.target

[Service]
User=prometheus
Group=prometheus
Type=simple
MemoryMax=3G
ExecStart=/usr/lib/prometheus/prometheus --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/var/lib/prometheus/ --web.listen-address=:9091 --storage.tsdb.retention.time=15d --web.console.templates=/usr/lib/prometheus/consoles --web.console.libraries=/usr/lib/prometheus/console_libraries

Restart=always

[Install]
WantedBy=multi-user.target
EOF

  sudo cp prometheus.service /etc/systemd/system/prometheus.service
  sudo chown prometheus:prometheus /etc/systemd/system/prometheus.service

  # Used for storing time-series database
  sudo mkdir -p /var/lib/prometheus
  sudo chown -R prometheus:prometheus /var/lib/prometheus

  sudo systemctl daemon-reload
  sudo systemctl start prometheus
  sudo systemctl status prometheus
  sudo systemctl enable prometheus
}

AMP_WORKSPACE_ID=$1
REGION=$(cat /mnt/var/lib/info/extraInstanceData.json | jq -r ".region")
IS_MASTER=$(cat /mnt/var/lib/info/instance.json | jq -r ".isMaster" | grep "true" || true)
COMPONENTS=`curl --retry 10 -s localhost:8321/configuration 2>/dev/null | jq '.componentNames'`

cd /tmp

if [ ! -z $IS_MASTER ]; then
  install_prometheus
fi

install_node_exporter
install_jmx_exporter

setup_jmx_hadoop

if echo $COMPONENTS | grep -q hbase; then
  setup_jmx_hbase
fi
